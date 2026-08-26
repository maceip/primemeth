"""ReasoningBank judge and distillation orchestration for persisted sessions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from local_memory.dedup import DedupResult, MemoryDeduplicator
from local_memory.distill import MemoryDistiller, parse_memory_response
from local_memory.embedder import LocalEmbedder
from local_memory.judge import JudgeResult, ModelCallable, TrajectoryJudge
from local_memory.prompts_reasoning_bank import (
    MAX_META_HEURISTICS,
    META_HEURISTIC_CONTRAST_SI,
)
from local_memory.retrieval import MemoryRetriever
from local_memory.store import MemoryStore, NewMemory
from middleware.telemetry import load_terminal_outcome
from middleware.terminal_outcome import TerminalOutcome
from synth.config import SynthSettings, load_synth_settings
from synth.model import configured_model_callable
from synth.protocol import JsonObject, JsonValue
from synth.session import SessionTrajectory, load_session_trajectory


class ReasoningBankError(RuntimeError):
    """Raised when a session cannot be judged or distilled safely."""


@dataclass(frozen=True, slots=True)
class JudgedSession:
    trajectory: SessionTrajectory
    settings: SynthSettings
    result: JudgeResult
    terminal_outcome: TerminalOutcome | None

    def as_dict(self) -> JsonObject:
        outcome_value = cast(
            JsonValue,
            self.terminal_outcome.as_dict()
            if self.terminal_outcome is not None
            else None,
        )
        return {
            "sessionId": self.trajectory.session_id,
            "leafId": self.trajectory.leaf_id,
            "verdict": self.result.verdict,
            "thoughts": self.result.thoughts,
            "taskState": self.trajectory.task_state,
            "taskQuery": self.trajectory.task_query,
            "terminalOutcome": outcome_value,
        }


@dataclass(frozen=True, slots=True)
class MetaHeuristicResult:
    outcomes: tuple[DedupResult, ...]
    parse_errors: tuple[str, ...]
    successful_memory_id: int | None


def _contrast_meta_heuristic(
    judged: JudgedSession,
    store: MemoryStore,
    model_call: ModelCallable,
    deduplicator: MemoryDeduplicator,
) -> MetaHeuristicResult:
    if judged.result.verdict != "failure":
        return MetaHeuristicResult((), (), None)
    stored_count = len(store.list())
    if stored_count == 0:
        return MetaHeuristicResult((), (), None)
    retrieved = MemoryRetriever(
        store,
        top_k=stored_count,
        min_score=judged.settings.memory.min_score,
    ).retrieve(judged.trajectory.task_query)
    successful = next(
        (
            result
            for result in retrieved
            if result.item.status == "success"
            and result.item.item_type == "strategy"
        ),
        None,
    )
    if successful is None:
        return MetaHeuristicResult((), (), None)
    prompt = (
        f"**Failed Query:** {judged.trajectory.task_query}\n\n"
        f"**Failed Trajectory:**\n{judged.trajectory.trajectory}\n\n"
        "**Retrieved Similar Successful Task:**\n"
        f"{successful.item.task_query}\n\n"
        "**Retrieved Successful Memory Evidence (not a full trajectory):**\n"
        f"Similarity: {successful.score:.6f}\n"
        f"Title: {successful.item.title}\n"
        f"Description: {successful.item.description}\n"
        f"Content: {successful.item.content}\n\n"
        f"**Failure Judgment:** {judged.result.thoughts}"
    )
    parsed = parse_memory_response(
        model_call(META_HEURISTIC_CONTRAST_SI, prompt)
    )
    accepted = parsed.items[:MAX_META_HEURISTICS]
    errors = list(parsed.errors)
    if len(parsed.items) > MAX_META_HEURISTICS:
        errors.append(
            f"response contains {len(parsed.items)} meta-heuristics; only the first "
            f"{MAX_META_HEURISTICS} was accepted"
        )
    outcomes = tuple(
        deduplicator.add(
            NewMemory(
                title=item.title,
                description=item.description,
                content=item.content,
                status="fail",
                item_type="meta_heuristic",
                task_query=judged.trajectory.task_query,
                session_id=judged.trajectory.session_id,
            )
        )
        for item in accepted
    )
    return MetaHeuristicResult(
        outcomes=outcomes,
        parse_errors=tuple(errors),
        successful_memory_id=successful.item.id,
    )


def judge_session(
    session_path: Path,
    *,
    exit_code: int | None = None,
    fixture_path: Path | None = None,
    wait_for_stability: bool = False,
) -> JudgedSession:
    trajectory = load_session_trajectory(
        session_path, wait_for_stability=wait_for_stability
    )
    settings = load_synth_settings(trajectory.workspace)
    if settings.kill_switch:
        raise ReasoningBankError("Synth kill switch is active")
    model_call = configured_model_callable(settings, fixture_path)
    terminal_outcome = load_terminal_outcome(
        settings.telemetry.directory, trajectory.session_id
    )
    result = TrajectoryJudge(model_call).judge(
        task_query=trajectory.task_query,
        trajectory=trajectory.trajectory,
        final_state=trajectory.final_state,
        response=trajectory.response,
        task_state=trajectory.task_state,
        exit_code=exit_code,
        terminal_outcome=terminal_outcome,
    )
    return JudgedSession(
        trajectory=trajectory,
        settings=settings,
        result=result,
        terminal_outcome=terminal_outcome,
    )


def distill_session(
    session_path: Path,
    *,
    exit_code: int | None = None,
    fixture_path: Path | None = None,
    wait_for_stability: bool = False,
) -> JsonObject:
    judged = judge_session(
        session_path,
        exit_code=exit_code,
        fixture_path=fixture_path,
        wait_for_stability=wait_for_stability,
    )
    settings = judged.settings
    if not settings.memory.enabled:
        raise ReasoningBankError("ReasoningBank memory is disabled in synth.json")
    embedder = LocalEmbedder(
        settings.workspace, model_name=settings.memory.embed_model
    )
    model_call = configured_model_callable(settings, fixture_path)
    with MemoryStore(
        settings.memory.database_path,
        embedder=embedder,
        workspace=settings.workspace,
    ) as store:
        deduplicator = MemoryDeduplicator(store)
        result = MemoryDistiller(
            model_call,
            deduplicator,
        ).distill(
            task_query=judged.trajectory.task_query,
            trajectory=judged.trajectory.trajectory,
            verdict=judged.result.verdict,
            session_id=judged.trajectory.session_id,
            template_key=None,
            item_type="strategy",
            judge_reasoning=judged.result.thoughts,
        )
        try:
            meta_result = _contrast_meta_heuristic(
                judged,
                store,
                model_call,
                deduplicator,
            )
        except Exception as error:
            meta_result = MetaHeuristicResult(
                outcomes=(),
                parse_errors=(
                    "contrastive meta-heuristic unavailable: "
                    f"{error.__class__.__name__}: {error}",
                ),
                successful_memory_id=None,
            )
    actions = [outcome.action for outcome in result.outcomes]
    meta_actions = [outcome.action for outcome in meta_result.outcomes]
    return {
        **judged.as_dict(),
        "inserted": result.inserted_count,
        "skipped": actions.count("skipped"),
        "merged": actions.count("merged"),
        "itemIds": [outcome.item.id for outcome in result.outcomes],
        "parseErrors": list(result.parse_errors),
        "metaHeuristicsInserted": meta_actions.count("inserted"),
        "metaHeuristicItemIds": [
            outcome.item.id for outcome in meta_result.outcomes
        ],
        "metaHeuristicParseErrors": list(meta_result.parse_errors),
        "contrastedSuccessfulMemoryId": meta_result.successful_memory_id,
    }


def distill_path(
    path: Path,
    *,
    exit_code: int | None = None,
    fixture_path: Path | None = None,
    wait_for_stability: bool = False,
) -> JsonObject:
    resolved = path.expanduser().resolve()
    sessions = (
        sorted(resolved.glob("*.jsonl"))
        if resolved.is_dir()
        else [resolved]
    )
    if not sessions:
        raise ReasoningBankError(f"no session JSONL files found under {resolved}")
    results = [
        distill_session(
            session,
            exit_code=exit_code,
            fixture_path=fixture_path,
            wait_for_stability=wait_for_stability,
        )
        for session in sessions
    ]
    inserted = 0
    meta_inserted = 0
    for result in results:
        value = result.get("inserted")
        if isinstance(value, int) and not isinstance(value, bool):
            inserted += value
        meta_value = result.get("metaHeuristicsInserted")
        if isinstance(meta_value, int) and not isinstance(meta_value, bool):
            meta_inserted += meta_value
    return {
        "sessions": len(results),
        "inserted": inserted,
        "metaHeuristicsInserted": meta_inserted,
        "results": cast(JsonValue, results),
    }
