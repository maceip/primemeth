"""Product wiring for BATS batch trajectories and candidate selection."""

from __future__ import annotations

import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from middleware.batch_driver import (
    BatchDriverConfig,
    BatchTaskSpec,
    VerificationRequest,
    VerifierResponse,
    parse_verification,
    run_batch,
)
from middleware.batch_select import (
    JudgeRequest,
    JudgeResult,
    select_candidates,
)
from synth.config import SynthSettings, load_synth_settings
from synth.model import configured_model_callable

_JUDGE_KEYS = {"scores", "selectedIndex", "justification"}
_JUDGE_SYSTEM_PROMPT = (
    "You are the Synth coding-candidate selector. Score every candidate against "
    "the original software-engineering task using only its diff and verifier "
    "evidence. Each score must be binary: 1 only when the patch is supported as "
    "satisfying the task, otherwise 0. When scores are mixed, select the strongest "
    "passing candidate. Return exactly one JSON object with keys scores, "
    "selectedIndex, and justification. selectedIndex is zero-based or null."
)


class BatchWiringError(RuntimeError):
    """Raised when the product batch path lacks a durable required artifact."""


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise BatchWiringError(f"{label} must be a JSON object")
    return cast(dict[str, object], value)


def _active_branch(path: Path) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            decoded: object = json.loads(line)
        except json.JSONDecodeError as error:
            raise BatchWiringError(
                f"session line {line_number} is invalid JSON: {path}"
            ) from error
        values.append(_mapping(decoded, f"session line {line_number}"))
    if not values or values[0].get("type") != "session":
        raise BatchWiringError(f"session has no v3 header: {path}")
    entries = values[1:]
    by_id: dict[str, dict[str, object]] = {}
    leaf_id: str | None = None
    for entry in entries:
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            raise BatchWiringError(f"session entry has no id: {path}")
        if entry_id in by_id:
            raise BatchWiringError(f"session has duplicate entry id {entry_id}")
        by_id[entry_id] = entry
        leaf_id = entry_id
    if leaf_id is None:
        raise BatchWiringError(f"session has no trajectory entries: {path}")
    reversed_branch: list[dict[str, object]] = []
    seen: set[str] = set()
    current: str | None = leaf_id
    while current is not None:
        if current in seen:
            raise BatchWiringError("session active branch contains a cycle")
        seen.add(current)
        current_entry = by_id.get(current)
        if current_entry is None:
            raise BatchWiringError(f"session parent entry is missing: {current}")
        reversed_branch.append(current_entry)
        parent = current_entry.get("parentId")
        if parent is not None and not isinstance(parent, str):
            raise BatchWiringError(f"session entry {current} has an invalid parentId")
        current = parent
    return list(reversed(reversed_branch))


class PersistedSessionVerifier:
    """Consume the extension-owned terminal verifier decision without a second call."""

    def __call__(self, request: VerificationRequest, /) -> VerifierResponse:
        session_path = request.trajectory.session_jsonl
        if session_path is None:
            raise BatchWiringError("trajectory has no session transcript")
        for entry in reversed(_active_branch(session_path)):
            if (
                entry.get("type") != "custom"
                or entry.get("customType") != "synth.verifier.decision"
            ):
                continue
            data = _mapping(entry.get("data"), "persisted verifier decision")
            payload: dict[str, object] = {
                "verification": data.get("verification"),
                "decision": data.get("decision"),
                "justification": data.get("justification"),
                "trajectory_summary": data.get("trajectorySummary"),
                "details": data.get("details"),
            }
            return parse_verification(payload)
        raise BatchWiringError(
            f"trajectory has no terminal synth.verifier.decision: {session_path}"
        )


def _judge_result(value: object, candidate_count: int) -> JudgeResult:
    mapping = _mapping(value, "candidate judge response")
    if set(mapping) != _JUDGE_KEYS:
        raise BatchWiringError(
            "candidate judge response must contain exactly scores, selectedIndex, "
            "and justification"
        )
    raw_scores = mapping["scores"]
    if not isinstance(raw_scores, list) or len(raw_scores) != candidate_count:
        raise BatchWiringError("candidate judge must score every candidate")
    scores: list[float] = []
    for value_score in raw_scores:
        if (
            not isinstance(value_score, (int, float))
            or isinstance(value_score, bool)
            or float(value_score) not in (0.0, 1.0)
        ):
            raise BatchWiringError("candidate judge scores must be binary")
        scores.append(float(value_score))
    selected = mapping["selectedIndex"]
    if selected is not None and (
        not isinstance(selected, int)
        or isinstance(selected, bool)
        or not 0 <= selected < candidate_count
    ):
        raise BatchWiringError("candidate judge selectedIndex is invalid")
    justification = mapping["justification"]
    if not isinstance(justification, str) or not justification.strip():
        raise BatchWiringError("candidate judge justification must be nonempty")
    return JudgeResult(
        scores=tuple(scores),
        selected_index=selected,
        response=justification.strip(),
    )


class LocalCandidateJudge:
    def __init__(
        self, settings: SynthSettings, fixture_path: Path | None = None
    ) -> None:
        self._settings = settings
        self._fixture_path = fixture_path

    def __call__(self, request: JudgeRequest, /) -> JudgeResult:
        if self._fixture_path is not None:
            try:
                value: object = json.loads(self._fixture_path.read_text())
            except (OSError, json.JSONDecodeError) as error:
                raise BatchWiringError(
                    f"cannot read candidate judge fixture {self._fixture_path}: {error}"
                ) from error
            return _judge_result(value, len(request.candidates))

        run_value = _mapping(
            json.loads((request.run_dir / "run.json").read_text()), "run.json"
        )
        task = run_value.get("prompt")
        if not isinstance(task, str) or not task:
            raise BatchWiringError("run.json has no task prompt")
        rendered_candidates = []
        for index, candidate in enumerate(request.candidates):
            rendered_candidates.append(
                "\n".join(
                    (
                        f"Candidate index {index} (artifact {candidate.number})",
                        "Verifier:",
                        json.dumps(candidate.verification, sort_keys=True),
                        "Diff:",
                        candidate.diff[-30_000:] or "<empty diff>",
                    )
                )
            )
        prompt = (
            f"Original task:\n{task}\n\nCandidates:\n\n"
            + "\n\n".join(rendered_candidates)
        )
        response = configured_model_callable(self._settings)(
            _JUDGE_SYSTEM_PROMPT, prompt
        )
        try:
            decoded: object = json.loads(response)
        except json.JSONDecodeError as error:
            raise BatchWiringError("candidate judge returned invalid JSON") from error
        return _judge_result(decoded, len(request.candidates))


def resolve_task(raw: str, workspace: Path) -> str:
    candidate = (workspace / raw).expanduser()
    task = candidate.read_text().strip() if candidate.is_file() else raw.strip()
    if not task:
        raise BatchWiringError("batch task must not be empty")
    return task


def default_run_id() -> str:
    return datetime.now(UTC).strftime("bats-%Y%m%dT%H%M%S%fZ")


def run_bats(
    *,
    workspace: Path,
    task: str,
    max_trajectories: int,
    base_ref: str,
    run_id: str,
    timeout_seconds: float,
    fixture_cli: Path | None,
    judge_fixture: Path | None,
) -> dict[str, object]:
    target = workspace.expanduser().resolve(strict=True)
    settings = load_synth_settings(target)
    if settings.kill_switch:
        raise BatchWiringError("Synth kill switch is active")
    if settings.verifier.max_cycles == 0:
        raise BatchWiringError("verifier.maxCycles must be positive for --bats")

    private_daemon_cli: Path | None = None
    if fixture_cli is not None:
        resolved_fixture_cli = fixture_cli.expanduser().resolve(strict=True)
        cli_argv = (sys.executable, str(resolved_fixture_cli))
    else:
        node = shutil.which("node")
        if node is None:
            raise BatchWiringError("node is required for Synth batch runs")
        private_daemon_cli = (
            Path(__file__).resolve().parents[1]
            / "packages"
            / "coding-agent"
            / "dist"
            / "cli.js"
        )
        if not private_daemon_cli.is_file():
            raise BatchWiringError(
                f"Prime Agent CLI is not built: {private_daemon_cli}"
            )
        extension = target / ".prime" / "agent" / "extensions" / "synth" / "index.ts"
        if not extension.is_file():
            raise BatchWiringError(
                f"target repository has no Synth extension: {extension}"
            )
        cli_argv = (node, str(private_daemon_cli))

    run_result = run_batch(
        BatchTaskSpec(
            target_repo=target,
            prompt=task,
            max_trajectories=max_trajectories,
            base_ref=base_ref,
        ),
        BatchDriverConfig(
            cli_argv=cli_argv,
            run_id=run_id,
            timeout_seconds=timeout_seconds,
            continue_generation=settings.verifier.continue_generation,
            min_budget_fraction=settings.verifier.min_budget_fraction,
            private_daemon_cli=private_daemon_cli,
        ),
        PersistedSessionVerifier(),
    )
    if run_result.candidates_recorded == 0:
        raise BatchWiringError(
            f"batch run produced no verified candidates: {run_result.run_dir}"
        )
    selection = select_candidates(
        run_result.run_dir,
        LocalCandidateJudge(settings, judge_fixture),
    )
    return {
        "runDir": str(run_result.run_dir),
        "trajectoriesRun": run_result.trajectories_run,
        "candidatesRecorded": run_result.candidates_recorded,
        "stopReason": run_result.stop_reason,
        "selection": str(selection.path),
        "selectedCandidate": selection.selected_candidate,
        "metrics": dict(selection.metrics),
    }
