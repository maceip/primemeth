"""Memory-aware test-time scaling over Prime Agent batch trajectories."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from local_memory.dedup import MemoryDeduplicator
from local_memory.distill import parse_memory_response
from local_memory.embedder import LocalEmbedder
from local_memory.judge import (
    JudgeResult,
    ModelCallable,
    TrajectoryJudge,
    parse_judge_response,
)
from local_memory.observation_redactor import cap_trajectory_for_prompt
from local_memory.prompts_reasoning_bank import (
    MATTS_CONTRASTIVE_SCALING_SI,
    MATTS_MAX_MEMORY_ITEMS,
    MATTS_SCALING_QUERY_FORMAT,
    MATTS_SCALING_TRAJECTORY_FORMAT,
    MATTS_SEQUENTIAL_REVERIFY_SI,
)
from local_memory.store import (
    Embedder,
    MemoryFeedbackOutcome,
    MemoryFeedbackSource,
    MemoryStatus,
    MemoryStore,
    NewMemory,
)
from middleware.batch_driver import BatchDriverConfig, BatchTaskSpec, run_batch
from middleware.terminal_outcome import TerminalOutcome
from synth.batch import PersistedSessionVerifier, resolve_agent_cli
from synth.config import load_synth_settings
from synth.model import configured_model_callable
from synth.protocol import JsonObject
from synth.session import SessionTrajectory, load_session_trajectory

_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,109}")


class MattsError(RuntimeError):
    """Raised when MaTTS cannot produce a complete contrastive induction."""


@dataclass(frozen=True, slots=True)
class ParallelTrajectory:
    sample: int
    run_dir: Path
    session_path: Path
    exit_code: int
    stop_reason: str
    terminal_outcome: TerminalOutcome
    started: float
    finished: float


@dataclass(frozen=True, slots=True)
class ReverifiedTrajectory:
    artifact: ParallelTrajectory
    trajectory: SessionTrajectory
    initial: JudgeResult
    reverified: JudgeResult


def default_matts_run_id() -> str:
    return datetime.now(UTC).strftime("matts-%Y%m%dT%H%M%S%fZ")


def run_matts(
    *,
    workspace: Path,
    task: str,
    k: int,
    base_ref: str,
    run_id: str,
    timeout_seconds: float,
    fixture_cli: Path | None,
    model_fixture: Path | None,
    embedder: Embedder | None = None,
) -> JsonObject:
    if k < 1:
        raise ValueError("k must be positive")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    normalized_task = task.strip()
    if not normalized_task:
        raise ValueError("task must not be empty")
    if _RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ValueError("run_id contains unsafe path characters or is too long")

    target = workspace.expanduser().resolve(strict=True)
    settings = load_synth_settings(target)
    if settings.kill_switch:
        raise MattsError("Synth kill switch is active")
    if not settings.memory.enabled:
        raise MattsError("ReasoningBank memory must be enabled for MaTTS")
    if settings.verifier.max_cycles == 0:
        raise MattsError("verifier.maxCycles must be positive for MaTTS")

    output_root = target / ".synth" / "matts" / run_id
    output_root.mkdir(parents=True, exist_ok=False)
    selected_embedder = embedder or LocalEmbedder(
        target, model_name=settings.memory.embed_model
    )
    with MemoryStore(
        settings.memory.database_path,
        embedder=selected_embedder,
        workspace=target,
    ) as store:
        input_memory_count = len(store.list())
    bank_snapshot = output_root / "bank-input.db"
    bank_sha256 = _snapshot_database(settings.memory.database_path, bank_snapshot)

    agent_cli = resolve_agent_cli(target, fixture_cli)
    trajectories = _run_parallel_trajectories(
        target=target,
        task=normalized_task,
        k=k,
        base_ref=base_ref,
        run_id=run_id,
        timeout_seconds=timeout_seconds,
        cli_argv=agent_cli.argv,
        private_daemon_cli=agent_cli.private_daemon_cli,
        bank_snapshot=bank_snapshot,
    )

    resolved_fixture = (
        model_fixture.expanduser().resolve(strict=True)
        if model_fixture is not None
        else None
    )
    model_call = configured_model_callable(settings, resolved_fixture)
    judged = _sequentially_reverify(
        trajectories,
        model_call,
        max_trajectory_tokens=settings.memory.max_trajectory_tokens,
    )
    contrast_prompt = _contrast_prompt(
        normalized_task,
        judged,
        max_trajectory_tokens=settings.memory.max_trajectory_tokens,
    )
    contrast_response = model_call(MATTS_CONTRASTIVE_SCALING_SI, contrast_prompt)
    parsed = parse_memory_response(contrast_response, max_items=MATTS_MAX_MEMORY_ITEMS)
    if not parsed.items:
        raise MattsError(
            "contrastive induction returned no valid memory items: "
            + "; ".join(parsed.errors)
        )

    memory_status: MemoryStatus = (
        "success"
        if any(item.reverified.verdict == "success" for item in judged)
        else "fail"
    )
    synthetic_session_id = f"matts:{run_id}"
    template_key = "matts:" + hashlib.sha256(normalized_task.encode()).hexdigest()[:16]
    with MemoryStore(
        settings.memory.database_path,
        embedder=selected_embedder,
        workspace=target,
    ) as store:
        feedback_results = []
        for item in judged:
            feedback_outcome, feedback_source = _trajectory_feedback_outcome(item)
            feedback_results.append(
                store.record_feedback(
                    session_id=item.trajectory.session_id,
                    memory_ids=item.trajectory.retrieved_memory_ids,
                    outcome=feedback_outcome,
                    source=feedback_source,
                )
            )
        deduplicator = MemoryDeduplicator(store)
        outcomes = tuple(
            deduplicator.add(
                NewMemory(
                    title=item.title,
                    description=item.description,
                    content=item.content,
                    status=memory_status,
                    item_type="strategy",
                    task_query=normalized_task,
                    session_id=synthetic_session_id,
                    template_key=template_key,
                )
            )
            for item in parsed.items
        )

    prompt_path = output_root / "contrast-prompt.md"
    response_path = output_root / "contrast-response.md"
    prompt_path.write_text(contrast_prompt)
    response_path.write_text(contrast_response)
    report: dict[str, object] = {
        "schemaVersion": 1,
        "runId": run_id,
        "task": normalized_task,
        "k": k,
        "parallelTrajectories": len(trajectories),
        "maxConcurrentTrajectories": _max_concurrency(trajectories),
        "sequentialReverificationRoundsPerTrajectory": 1,
        "inputBank": {
            "path": str(bank_snapshot.relative_to(target)),
            "sha256": bank_sha256,
            "itemCount": input_memory_count,
        },
        "trajectories": [_trajectory_report(target, item) for item in judged],
        "contrast": {
            "prompt": str(prompt_path.relative_to(target)),
            "response": str(response_path.relative_to(target)),
            "parseErrors": list(parsed.errors),
        },
        "memory": {
            "status": memory_status,
            "inserted": sum(outcome.action == "inserted" for outcome in outcomes),
            "skipped": sum(outcome.action == "skipped" for outcome in outcomes),
            "merged": sum(outcome.action == "merged" for outcome in outcomes),
            "itemIds": [outcome.item.id for outcome in outcomes],
            "actions": [outcome.action for outcome in outcomes],
            "templateKey": template_key,
            "feedback": [
                {
                    "sessionId": item.trajectory.session_id,
                    "outcome": feedback.outcome,
                    "source": feedback.source,
                    "updatedItemIds": list(feedback.updated_item_ids),
                    "unchangedItemIds": list(feedback.unchanged_item_ids),
                    "missingItemIds": list(feedback.missing_item_ids),
                    "ineligibleItemIds": list(feedback.ineligible_item_ids),
                }
                for item, feedback in zip(judged, feedback_results, strict=True)
            ],
        },
    }
    report_path = output_root / "report.json"
    _write_json(report_path, report)
    return cast(JsonObject, {**report, "reportPath": str(report_path)})


def _trajectory_feedback_outcome(
    item: ReverifiedTrajectory,
) -> tuple[MemoryFeedbackOutcome, MemoryFeedbackSource]:
    reward = item.artifact.terminal_outcome.reward
    if reward is not None:
        return ("success" if reward == 1 else "failure"), "terminal"
    return item.reverified.verdict, "judge"


def _run_parallel_trajectories(
    *,
    target: Path,
    task: str,
    k: int,
    base_ref: str,
    run_id: str,
    timeout_seconds: float,
    cli_argv: tuple[str, ...],
    private_daemon_cli: Path | None,
    bank_snapshot: Path,
) -> tuple[ParallelTrajectory, ...]:
    futures: dict[Future[ParallelTrajectory], int] = {}
    with ThreadPoolExecutor(max_workers=k, thread_name_prefix="synth-matts") as pool:
        for sample in range(1, k + 1):
            future = pool.submit(
                _run_parallel_trajectory,
                target=target,
                task=task,
                sample=sample,
                base_ref=base_ref,
                run_id=run_id,
                timeout_seconds=timeout_seconds,
                cli_argv=cli_argv,
                private_daemon_cli=private_daemon_cli,
                bank_snapshot=bank_snapshot,
            )
            futures[future] = sample
        completed: dict[int, ParallelTrajectory] = {}
        for future in as_completed(futures):
            sample = futures[future]
            try:
                completed[sample] = future.result()
            except Exception as error:
                raise MattsError(
                    f"parallel trajectory {sample} failed: "
                    f"{error.__class__.__name__}: {error}"
                ) from error
    return tuple(completed[sample] for sample in range(1, k + 1))


def _run_parallel_trajectory(
    *,
    target: Path,
    task: str,
    sample: int,
    base_ref: str,
    run_id: str,
    timeout_seconds: float,
    cli_argv: tuple[str, ...],
    private_daemon_cli: Path | None,
    bank_snapshot: Path,
) -> ParallelTrajectory:
    started = time.monotonic()
    sample_run_id = f"{run_id}-sample-{sample:03d}"
    result = run_batch(
        BatchTaskSpec(
            target_repo=target,
            prompt=task,
            max_trajectories=1,
            base_ref=base_ref,
        ),
        BatchDriverConfig(
            cli_argv=cli_argv,
            run_id=sample_run_id,
            timeout_seconds=timeout_seconds,
            env={"SYNTH_BATCH_READ_ONLY_MEMORY": "1"},
            continue_generation=False,
            private_daemon_cli=private_daemon_cli,
            memory_db_seed=bank_snapshot,
        ),
        PersistedSessionVerifier(),
    )
    finished = time.monotonic()
    metadata_path = result.run_dir / "trajectories" / "1" / "trajectory.json"
    metadata = _mapping(
        json.loads(metadata_path.read_text()), f"trajectory {sample} metadata"
    )
    session_value = metadata.get("session_jsonl")
    if not isinstance(session_value, str) or not session_value:
        raise MattsError(f"parallel trajectory {sample} has no session transcript")
    exit_code = metadata.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        raise MattsError(f"parallel trajectory {sample} has no integer exit code")
    return ParallelTrajectory(
        sample=sample,
        run_dir=result.run_dir,
        session_path=result.run_dir / session_value,
        exit_code=exit_code,
        stop_reason=result.stop_reason,
        terminal_outcome=result.terminal_outcome,
        started=started,
        finished=finished,
    )


def _sequentially_reverify(
    artifacts: tuple[ParallelTrajectory, ...],
    model_call: ModelCallable,
    *,
    max_trajectory_tokens: int,
) -> tuple[ReverifiedTrajectory, ...]:
    judge = TrajectoryJudge(
        model_call,
        max_trajectory_tokens=max_trajectory_tokens,
    )
    results: list[ReverifiedTrajectory] = []
    for artifact in artifacts:
        trajectory = load_session_trajectory(
            artifact.session_path,
            redact_observations=True,
        )
        initial = judge.judge(
            task_query=trajectory.task_query,
            trajectory=trajectory.trajectory,
            final_state=trajectory.final_state,
            response=trajectory.response,
            task_state=trajectory.task_state,
            exit_code=artifact.exit_code,
            terminal_outcome=artifact.terminal_outcome,
        )
        reverified = _reverify(
            trajectory,
            initial,
            model_call,
            max_trajectory_tokens=max_trajectory_tokens,
        )
        results.append(
            ReverifiedTrajectory(
                artifact=artifact,
                trajectory=trajectory,
                initial=initial,
                reverified=reverified,
            )
        )
    return tuple(results)


def _reverify(
    trajectory: SessionTrajectory,
    initial: JudgeResult,
    model_call: ModelCallable,
    *,
    max_trajectory_tokens: int,
) -> JudgeResult:
    prompt_trajectory = cap_trajectory_for_prompt(
        trajectory.trajectory,
        max_tokens=max_trajectory_tokens,
    )
    prompt = (
        f"**Task:** {trajectory.task_query}\n\n"
        f"**Trajectory:**\n{prompt_trajectory}\n\n"
        f"**Initial Judgment:**\n{initial.raw_response}"
    )
    raw_response = model_call(MATTS_SEQUENTIAL_REVERIFY_SI, prompt)
    verdict, thoughts = parse_judge_response(raw_response)
    return JudgeResult(
        verdict=verdict,
        thoughts=thoughts,
        raw_response=raw_response,
        prompt=prompt,
    )


def _contrast_prompt(
    task: str,
    trajectories: tuple[ReverifiedTrajectory, ...],
    *,
    max_trajectory_tokens: int,
) -> str:
    rendered = MATTS_SCALING_QUERY_FORMAT.format(query=task)
    per_trajectory_tokens = max(
        1,
        max_trajectory_tokens // max(1, len(trajectories)),
    )
    for item in trajectories:
        prompt_trajectory = cap_trajectory_for_prompt(
            item.trajectory.trajectory,
            max_tokens=per_trajectory_tokens,
        )
        steps = (
            f"{prompt_trajectory}\n\n"
            f"**Initial Judgment:** {item.initial.verdict}\n"
            f"{item.initial.thoughts}\n\n"
            f"**Sequential Re-verification:** {item.reverified.verdict}\n"
            f"{item.reverified.thoughts}"
        )
        rendered += MATTS_SCALING_TRAJECTORY_FORMAT.format(
            sample_number=item.artifact.sample,
            steps=steps,
        )
    return rendered


def _trajectory_report(target: Path, item: ReverifiedTrajectory) -> dict[str, object]:
    return {
        "sample": item.artifact.sample,
        "runDir": str(item.artifact.run_dir.relative_to(target)),
        "sessionJsonl": str(item.artifact.session_path.relative_to(target)),
        "sessionId": item.trajectory.session_id,
        "agentExitCode": item.artifact.exit_code,
        "batchStopReason": item.artifact.stop_reason,
        "terminalOutcome": item.artifact.terminal_outcome.as_dict(),
        "durationSeconds": item.artifact.finished - item.artifact.started,
        "initialJudgment": {
            "verdict": item.initial.verdict,
            "thoughts": item.initial.thoughts,
        },
        "reverifiedJudgment": {
            "verdict": item.reverified.verdict,
            "thoughts": item.reverified.thoughts,
        },
    }


def _max_concurrency(trajectories: tuple[ParallelTrajectory, ...]) -> int:
    events = [
        event
        for item in trajectories
        for event in ((item.started, 1), (item.finished, -1))
    ]
    active = 0
    maximum = 0
    for _timestamp, delta in sorted(events, key=lambda item: (item[0], -item[1])):
        active += delta
        maximum = max(maximum, active)
    return maximum


def _snapshot_database(source: Path, destination: Path) -> str:
    source_connection = sqlite3.connect(
        f"{source.resolve(strict=True).as_uri()}?mode=ro", uri=True, timeout=5.0
    )
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise MattsError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
