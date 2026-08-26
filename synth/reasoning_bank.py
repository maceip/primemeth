"""ReasoningBank judge and distillation orchestration for persisted sessions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from local_memory.dedup import MemoryDeduplicator
from local_memory.distill import MemoryDistiller
from local_memory.embedder import LocalEmbedder
from local_memory.judge import JudgeResult, TrajectoryJudge
from local_memory.store import MemoryStore
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

    def as_dict(self) -> JsonObject:
        return {
            "sessionId": self.trajectory.session_id,
            "leafId": self.trajectory.leaf_id,
            "verdict": self.result.verdict,
            "thoughts": self.result.thoughts,
            "taskState": self.trajectory.task_state,
            "taskQuery": self.trajectory.task_query,
        }


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
    result = TrajectoryJudge(model_call).judge(
        task_query=trajectory.task_query,
        trajectory=trajectory.trajectory,
        final_state=trajectory.final_state,
        response=trajectory.response,
        task_state=trajectory.task_state,
        exit_code=exit_code,
    )
    return JudgedSession(trajectory=trajectory, settings=settings, result=result)


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
        result = MemoryDistiller(
            model_call,
            MemoryDeduplicator(store),
        ).distill(
            task_query=judged.trajectory.task_query,
            trajectory=judged.trajectory.trajectory,
            verdict=judged.result.verdict,
            session_id=judged.trajectory.session_id,
            template_key=None,
            item_type="strategy",
            judge_reasoning=judged.result.thoughts,
        )
    actions = [outcome.action for outcome in result.outcomes]
    return {
        **judged.as_dict(),
        "inserted": result.inserted_count,
        "skipped": actions.count("skipped"),
        "merged": actions.count("merged"),
        "itemIds": [outcome.item.id for outcome in result.outcomes],
        "parseErrors": list(result.parse_errors),
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
    for result in results:
        value = result.get("inserted")
        if isinstance(value, int) and not isinstance(value, bool):
            inserted += value
    return {
        "sessions": len(results),
        "inserted": inserted,
        "results": cast(JsonValue, results),
    }
