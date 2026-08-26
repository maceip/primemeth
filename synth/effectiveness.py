"""Paired, acceptance-scored memory effectiveness evaluation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import Literal, cast

import yaml  # type: ignore[import-untyped]

from middleware.batch_driver import BatchDriverConfig, BatchTaskSpec, run_batch
from middleware.terminal_outcome import TerminalOutcome, TerminationReason
from synth.batch import PersistedSessionVerifier
from synth.config import EndpointApi

Condition = Literal["memory_off", "memory_on"]
_CONDITIONS: tuple[Condition, Condition] = ("memory_off", "memory_on")

_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_TASK_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_ACCEPTANCE_RUNNER_SOURCE = """from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


def main() -> int:
    workspace_value = os.environ.get("SYNTH_ACCEPTANCE_WORKTREE")
    if workspace_value is None:
        raise RuntimeError("SYNTH_ACCEPTANCE_WORKTREE is required")
    workspace = Path(workspace_value).resolve(strict=True)
    root = Path(__file__).resolve().parent
    tests = sorted(root.glob("test_*.py"))
    if not tests:
        raise RuntimeError("immutable acceptance suite contains no tests")
    sys.path.insert(0, str(workspace))
    return int(
        pytest.main(
            [
                "-q",
                "-p",
                "no:cacheprovider",
                "--rootdir",
                str(root),
                "--confcutdir",
                str(root),
                "--import-mode=prepend",
                *(str(path) for path in tests),
            ]
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
"""


class EffectivenessError(RuntimeError):
    """Raised when a paired effectiveness run cannot produce honest evidence."""


@dataclass(frozen=True, slots=True)
class EndpointSpec:
    base_url: str
    api: EndpointApi
    model: str
    api_key_env: str


@dataclass(frozen=True, slots=True)
class TaskFixture:
    task_id: str
    prompt: str
    root: Path
    test_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SessionMetrics:
    tool_calls: int
    tool_names: tuple[str, ...]
    total_tokens: int
    cost_usd: float
    memory_item_ids: tuple[int, ...]
    apis: tuple[str, ...]
    providers: tuple[str, ...]
    models: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    commit: str
    status: tuple[str, ...]
    workspace_content_sha256: str
    tracked_diff_sha256: str
    untracked_files: tuple[tuple[str, str], ...]
    source_file_count: int
    runtime_cli_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "commit": self.commit,
            "status": list(self.status),
            "workspaceContentSha256": self.workspace_content_sha256,
            "trackedDiffSha256": self.tracked_diff_sha256,
            "untrackedFiles": dict(self.untracked_files),
            "sourceFileCount": self.source_file_count,
            "runtimeCliSha256": self.runtime_cli_sha256,
        }


@dataclass(frozen=True, slots=True)
class TrialOutcome:
    ordinal: int
    repetition: int
    task_id: str
    condition: Condition
    terminal_outcome: TerminalOutcome
    agent_exit_code: int
    acceptance_exit_code: int | None
    acceptance_timed_out: bool
    acceptance_integrity_failures: tuple[str, ...]
    batch_stop_reason: str
    tool_calls: int
    tool_names: tuple[str, ...]
    total_tokens: int
    cost_usd: float
    memory_item_ids: tuple[int, ...]
    apis: tuple[str, ...]
    providers: tuple[str, ...]
    models: tuple[str, ...]
    session_jsonl: Path | None
    artifact_dir: Path
    diff_sha256: str

    @property
    def success(self) -> bool:
        return self.terminal_outcome.succeeded

    def as_dict(self, report_root: Path) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "repetition": self.repetition,
            "taskId": self.task_id,
            "condition": self.condition,
            "success": self.success,
            "terminalOutcome": self.terminal_outcome.as_dict(),
            "agentExitCode": self.agent_exit_code,
            "acceptanceExitCode": self.acceptance_exit_code,
            "acceptanceTimedOut": self.acceptance_timed_out,
            "acceptanceIntegrityFailures": list(self.acceptance_integrity_failures),
            "batchStopReason": self.batch_stop_reason,
            "toolCalls": self.tool_calls,
            "toolNames": list(self.tool_names),
            "totalTokens": self.total_tokens,
            "costUsd": self.cost_usd,
            "memoryItemIds": list(self.memory_item_ids),
            "memoryExposed": bool(self.memory_item_ids),
            "apis": list(self.apis),
            "providers": list(self.providers),
            "models": list(self.models),
            "diffSha256": self.diff_sha256,
            "sessionJsonl": (
                str(self.session_jsonl.relative_to(report_root))
                if self.session_jsonl is not None
                else None
            ),
            "artifactDir": str(self.artifact_dir.relative_to(report_root)),
        }


def default_effectiveness_run_id() -> str:
    return datetime.now(UTC).strftime("memory-ab-%Y%m%dT%H%M%S%fZ")


def load_task_fixtures(
    root: Path, selected_cases: tuple[str, ...] = ()
) -> tuple[TaskFixture, ...]:
    resolved = root.expanduser().resolve(strict=True)
    selected = set(selected_cases)
    fixtures: list[TaskFixture] = []
    for manifest_path in sorted(resolved.glob("*/task.yaml")):
        raw: object = yaml.safe_load(manifest_path.read_text())
        mapping = _mapping(raw, f"task manifest {manifest_path}")
        task_id = _text(mapping.get("id"), f"{manifest_path}: id")
        if _TASK_ID_PATTERN.fullmatch(task_id) is None:
            raise EffectivenessError(f"unsafe task id in {manifest_path}: {task_id}")
        if selected and task_id not in selected:
            continue
        prompt = _text(mapping.get("prompt"), f"{manifest_path}: prompt")
        test_files = tuple(
            path.name for path in sorted(manifest_path.parent.glob("test_*.py"))
        )
        if not test_files:
            raise EffectivenessError(f"task has no focused pytest file: {task_id}")
        fixtures.append(
            TaskFixture(
                task_id=task_id,
                prompt=prompt,
                root=manifest_path.parent,
                test_files=test_files,
            )
        )
    found = {fixture.task_id for fixture in fixtures}
    missing = selected - found
    if missing:
        raise EffectivenessError(
            f"unknown selected task cases: {', '.join(sorted(missing))}"
        )
    if not fixtures:
        raise EffectivenessError(f"no task fixtures found under {resolved}")
    return tuple(fixtures)


def snapshot_memory_database(source: Path, destination: Path) -> str:
    resolved_source = source.expanduser().resolve(strict=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise EffectivenessError(f"memory snapshot already exists: {destination}")
    source_connection = sqlite3.connect(
        f"{resolved_source.as_uri()}?mode=ro", uri=True, timeout=5.0
    )
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def session_metrics(path: Path) -> SessionMetrics:
    entries = _active_session_entries(path)
    tool_calls = 0
    tool_names: list[str] = []
    total_tokens = 0
    cost_usd = 0.0
    memory_item_ids: list[int] = []
    apis: list[str] = []
    providers: list[str] = []
    models: list[str] = []
    for entry in entries:
        if (
            entry.get("type") == "custom"
            and entry.get("customType") == "synth.memory.retrieval"
        ):
            data = _mapping(entry.get("data"), "memory retrieval receipt")
            raw_item_ids = data.get("itemIds")
            if isinstance(raw_item_ids, list):
                memory_item_ids.extend(
                    value
                    for value in raw_item_ids
                    if isinstance(value, int)
                    and not isinstance(value, bool)
                    and value > 0
                )
        if entry.get("type") != "message":
            continue
        message = entry.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "toolCall":
                    continue
                tool_calls += 1
                name = part.get("name")
                if isinstance(name, str) and name:
                    tool_names.append(name)
        for field, target in (
            ("api", apis),
            ("provider", providers),
            ("model", models),
        ):
            value = message.get(field)
            if isinstance(value, str) and value:
                target.append(value)
        usage = message.get("usage")
        if usage is None:
            continue
        usage_mapping = _mapping(usage, "assistant usage")
        total_tokens += _nonnegative_integer(
            usage_mapping.get("totalTokens"), "usage.totalTokens"
        )
        cost = _mapping(usage_mapping.get("cost"), "usage.cost")
        cost_usd += _nonnegative_number(cost.get("total"), "usage.cost.total")
    return SessionMetrics(
        tool_calls=tool_calls,
        tool_names=tuple(tool_names),
        total_tokens=total_tokens,
        cost_usd=cost_usd,
        memory_item_ids=tuple(dict.fromkeys(memory_item_ids)),
        apis=tuple(dict.fromkeys(apis)),
        providers=tuple(dict.fromkeys(providers)),
        models=tuple(dict.fromkeys(models)),
    )


def run_effectiveness_evaluation(
    *,
    source_root: Path,
    tasks_root: Path,
    bank_path: Path,
    model: str,
    thinking: str,
    endpoint: EndpointSpec,
    repetitions: int,
    selected_cases: tuple[str, ...] = (),
    run_id: str | None = None,
    timeout_seconds: float = 900.0,
) -> dict[str, object]:
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    selected_run_id = run_id or default_effectiveness_run_id()
    if _RUN_ID_PATTERN.fullmatch(selected_run_id) is None:
        raise ValueError("run_id contains unsafe path characters")
    root = source_root.expanduser().resolve(strict=True)
    fixtures = load_task_fixtures(tasks_root, selected_cases)
    cli_path = root / "packages" / "coding-agent" / "dist" / "cli.js"
    overlay_python = root / ".venv" / "bin" / "python"
    node = shutil.which("node")
    if node is None:
        raise EffectivenessError("node is required for effectiveness evaluation")
    if not cli_path.is_file():
        raise EffectivenessError(f"Prime Agent CLI is not built: {cli_path}")
    if not overlay_python.is_file():
        raise EffectivenessError(f"overlay Python is not prepared: {overlay_python}")

    source_snapshot = _capture_source_snapshot(root, cli_path)

    output_root = root / ".synth" / "effectiveness" / selected_run_id
    output_root.mkdir(parents=True, exist_ok=False)
    bank_snapshot = output_root / "bank-snapshot.db"
    bank_hash = snapshot_memory_database(bank_path, bank_snapshot)
    bank_item_count = _memory_item_count(bank_snapshot)
    if bank_item_count == 0:
        raise EffectivenessError("memory bank contains no items")
    verifier_fixture = output_root / "verifier.json"
    _write_json(
        verifier_fixture,
        {
            "verifications": [
                {
                    "verification": (
                        "Deterministic external acceptance is authoritative for this "
                        "evaluation trajectory."
                    ),
                    "decision": "SUCCESS",
                    "justification": (
                        "The batch driver applies the focused pytest gate."
                    ),
                    "trajectory_summary": (
                        "The coding trajectory reached its final answer."
                    ),
                    "details": {},
                }
            ]
        },
    )
    verifier_hash = hashlib.sha256(verifier_fixture.read_bytes()).hexdigest()

    trials: list[TrialOutcome] = []
    ordinal = 0
    for repetition in range(1, repetitions + 1):
        condition_order: tuple[Condition, Condition] = (
            ("memory_off", "memory_on")
            if repetition % 2 == 1
            else ("memory_on", "memory_off")
        )
        for fixture in fixtures:
            for condition in condition_order:
                ordinal += 1
                trials.append(
                    _run_trial(
                        root=root,
                        output_root=output_root,
                        fixture=fixture,
                        condition=condition,
                        repetition=repetition,
                        ordinal=ordinal,
                        model=model,
                        thinking=thinking,
                        endpoint=endpoint,
                        overlay_python=overlay_python,
                        node=node,
                        cli_path=cli_path,
                        bank_snapshot=bank_snapshot,
                        verifier_fixture=verifier_fixture,
                        timeout_seconds=timeout_seconds,
                    )
                )

    final_source_snapshot = _capture_source_snapshot(root, cli_path)
    if final_source_snapshot != source_snapshot:
        raise EffectivenessError(
            "source or built CLI changed while the effectiveness run was active"
        )

    report: dict[str, object] = {
        "schemaVersion": 2,
        "complete": _evaluation_complete(trials),
        "runId": selected_run_id,
        "createdAt": datetime.now(UTC).isoformat(),
        "model": model,
        "thinking": thinking,
        "endpointApi": endpoint.api,
        "taskRoot": str(tasks_root.expanduser().resolve()),
        "taskIds": [fixture.task_id for fixture in fixtures],
        "repetitions": repetitions,
        "bankSnapshot": str(bank_snapshot.relative_to(output_root)),
        "bankSha256": bank_hash,
        "bankItemCount": bank_item_count,
        "verifierFixtureSha256": verifier_hash,
        "sourceCommit": source_snapshot.commit,
        "sourceStatus": list(source_snapshot.status),
        "sourceSnapshot": source_snapshot.as_dict(),
        "groundTruth": (
            "focused pytest exit status from a hashed, read-only external suite"
        ),
        "usageScope": "main-agent native session usage only",
        "interpretation": "descriptive paired pilot",
        "unmeasuredControlPlane": [
            "ReasoningBank embedding retrieval runs locally and has no token cost.",
            "Verifier inference is replaced by a deterministic fixture.",
            (
                "Judge and distillation calls used to build the bank are separate "
                "evidence."
            ),
        ],
        "conditions": {
            condition: _aggregate(
                [trial for trial in trials if trial.condition == condition]
            )
            for condition in _CONDITIONS
        },
        "pairedDeltas": _paired_deltas(trials),
        "trials": [trial.as_dict(output_root) for trial in trials],
        "limitations": [
            (
                "The fixture tasks are intentionally small and may exhibit a ceiling "
                "effect."
            ),
            "This paired pilot measures observed runs, not statistical significance.",
            (
                "A memory-on trial without a persisted retrieval receipt is reported "
                "as unexposed."
            ),
        ],
    }
    report_path = output_root / "report.json"
    _write_json(report_path, report)
    return {**report, "reportPath": str(report_path)}


def _run_trial(
    *,
    root: Path,
    output_root: Path,
    fixture: TaskFixture,
    condition: Condition,
    repetition: int,
    ordinal: int,
    model: str,
    thinking: str,
    endpoint: EndpointSpec,
    overlay_python: Path,
    node: str,
    cli_path: Path,
    bank_snapshot: Path,
    verifier_fixture: Path,
    timeout_seconds: float,
) -> TrialOutcome:
    trial_dir = output_root / "trials" / f"{ordinal:03d}-{fixture.task_id}-{condition}"
    repo = trial_dir / "repo"
    _materialize_trial_repo(
        root=root,
        repo=repo,
        fixture=fixture,
        memory_enabled=condition == "memory_on",
        endpoint=endpoint,
    )
    acceptance_runner, acceptance_inputs = _prepare_acceptance_suite(
        fixture, trial_dir / "acceptance"
    )
    immutable_worktree_paths = {
        relative_path: _file_sha256(repo / relative_path)
        for relative_path in fixture.test_files
    }
    prompt = (
        f"{fixture.prompt}\n\n"
        "Work only in this repository. Inspect the implementation and focused tests, "
        "make the smallest correct fix, run the focused test, and report the evidence."
    )
    result = run_batch(
        BatchTaskSpec(target_repo=repo, prompt=prompt, max_trajectories=1),
        BatchDriverConfig(
            cli_argv=(node, str(cli_path)),
            cli_config_args=(
                "--offline",
                "--model",
                model,
                "--thinking",
                thinking,
                "--tools",
                "ipython",
                "--no-skills",
                "--no-context-files",
            ),
            run_id=f"trial-{ordinal:03d}",
            timeout_seconds=timeout_seconds,
            env={
                "PATH": f"{root / '.venv' / 'bin'}:{os.environ.get('PATH', '')}",
                "UV_PROJECT_ENVIRONMENT": str(root / ".venv"),
                "SYNTH_FASTEMBED_CACHE_DIR": str(
                    root / ".synth" / "models" / "fastembed"
                ),
                "SYNTH_VERIFIER_FIXTURE": str(verifier_fixture),
                "SYNTH_BATCH_READ_ONLY_MEMORY": "1",
            },
            continue_generation=False,
            private_daemon_cli=cli_path,
            acceptance_argv=(
                str(overlay_python),
                "-I",
                str(acceptance_runner),
            ),
            acceptance_inputs=acceptance_inputs,
            immutable_worktree_paths=immutable_worktree_paths,
            memory_db_seed=(bank_snapshot if condition == "memory_on" else None),
        ),
        PersistedSessionVerifier(),
    )
    artifact_dir = result.run_dir / "trajectories" / "1"
    metadata = _mapping(
        json.loads((artifact_dir / "trajectory.json").read_text()),
        "trajectory metadata",
    )
    acceptance_exit_code = _optional_integer(metadata.get("acceptance_exit_code"))
    acceptance_timed_out = _boolean(
        metadata.get("acceptance_timed_out"), "trajectory acceptance_timed_out"
    )
    acceptance_integrity_failures = _string_tuple(
        metadata.get("acceptance_integrity_failures"),
        "trajectory acceptance_integrity_failures",
    )
    agent_exit_code = _nonnegative_integer(
        metadata.get("exit_code"), "trajectory exit_code"
    )
    try:
        metadata_outcome = TerminalOutcome.from_mapping(
            metadata.get("terminal_outcome")
        )
    except ValueError as error:
        raise EffectivenessError(
            f"trajectory terminal outcome is invalid: {error}"
        ) from error
    if metadata_outcome != result.terminal_outcome:
        raise EffectivenessError(
            "trajectory and run result disagree about the terminal outcome"
        )
    session_value = metadata.get("session_jsonl")
    session_path = (
        result.run_dir / session_value
        if isinstance(session_value, str) and session_value
        else None
    )
    metrics = (
        session_metrics(session_path)
        if session_path is not None
        else SessionMetrics(0, (), 0, 0.0, (), (), (), ())
    )
    return TrialOutcome(
        ordinal=ordinal,
        repetition=repetition,
        task_id=fixture.task_id,
        condition=condition,
        terminal_outcome=result.terminal_outcome,
        agent_exit_code=agent_exit_code,
        acceptance_exit_code=acceptance_exit_code,
        acceptance_timed_out=acceptance_timed_out,
        acceptance_integrity_failures=acceptance_integrity_failures,
        batch_stop_reason=result.stop_reason,
        tool_calls=metrics.tool_calls,
        tool_names=metrics.tool_names,
        total_tokens=metrics.total_tokens,
        cost_usd=metrics.cost_usd,
        memory_item_ids=metrics.memory_item_ids,
        apis=metrics.apis,
        providers=metrics.providers,
        models=metrics.models,
        session_jsonl=session_path,
        artifact_dir=artifact_dir,
        diff_sha256=hashlib.sha256(
            (artifact_dir / "diff.patch").read_bytes()
        ).hexdigest(),
    )


def _prepare_acceptance_suite(
    fixture: TaskFixture, destination: Path
) -> tuple[Path, dict[Path, str]]:
    destination.mkdir(parents=True, exist_ok=False)
    test_paths: list[Path] = []
    for relative_path in fixture.test_files:
        source = fixture.root / relative_path
        if source.is_symlink() or not source.is_file():
            raise EffectivenessError(
                f"acceptance test must be a regular file: {source}"
            )
        target = destination / relative_path
        shutil.copy2(source, target)
        test_paths.append(target)
    runner = destination / "run_acceptance.py"
    runner.write_text(_ACCEPTANCE_RUNNER_SOURCE)
    inputs = (runner, *test_paths)
    digests = {path: _file_sha256(path) for path in inputs}
    for path in inputs:
        path.chmod(0o444)
    destination.chmod(0o555)
    return runner, digests


def _materialize_trial_repo(
    *,
    root: Path,
    repo: Path,
    fixture: TaskFixture,
    memory_enabled: bool,
    endpoint: EndpointSpec,
) -> None:
    repo.mkdir(parents=True)
    for source in sorted(fixture.root.iterdir()):
        if source.name == "task.yaml" or not source.is_file() or source.is_symlink():
            continue
        shutil.copy2(source, repo / source.name)
    if (repo / "task.yaml").exists():
        raise EffectivenessError("task manifest leaked into evaluation repository")
    extension_source = root / ".prime" / "agent" / "extensions" / "synth"
    if not extension_source.is_dir():
        raise EffectivenessError(f"Synth extension is missing: {extension_source}")
    shutil.copytree(
        extension_source,
        repo / ".prime" / "agent" / "extensions" / "synth",
    )
    (repo / ".gitignore").write_text(".synth/\n.pytest_cache/\n__pycache__/\n*.pyc\n")
    (repo / "pyproject.toml").write_text(
        "[project]\n"
        f'name = "synth-effectiveness-{fixture.task_id}"\n'
        'version = "0.0.0"\n'
        'requires-python = ">=3.12,<3.15"\n'
        "dependencies = []\n"
    )
    _write_json(
        repo / "synth.json",
        {
            "endpoint": {
                "baseUrl": endpoint.base_url,
                "model": endpoint.model,
                "smallModel": endpoint.model,
                "apiKeyEnv": endpoint.api_key_env,
                "api": endpoint.api,
            },
            "telemetry": {"dir": ".synth/telemetry"},
            "budgets": {
                "bash": 20,
                "edit": 20,
                "ipython": 20,
                "subagents": 0,
                "tokens": 0,
                "costUsd": 0,
            },
            "memory": {
                "enabled": memory_enabled,
                "k": 3,
                "minScore": 0.0,
                "db": ".synth/synth.db",
                "embedModel": "BAAI/bge-small-en-v1.5",
                "judgeModel": "small",
            },
            "verifier": {
                "interactive": False,
                "batch": True,
                "maxCycles": 1,
                "contGen": {"enabled": False, "minBudgetFrac": 0.2},
            },
            "skillCompiler": {
                "enabled": False,
                "maxPerSession": 0,
                "quarantine": ".synth/quarantine",
            },
            "killSwitch": {"all": False},
        },
    )
    _run("git", "init", "-q", str(repo))
    paths = [
        str(path.relative_to(repo))
        for path in sorted(repo.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(repo).parts
    ]
    _run("git", "-C", str(repo), "add", "--", *paths)
    _run(
        "git",
        "-C",
        str(repo),
        "-c",
        "user.name=Synth Effectiveness",
        "-c",
        "user.email=synth-effectiveness@example.invalid",
        "commit",
        "-q",
        "-m",
        "fixture base",
    )


def _active_session_entries(path: Path) -> list[dict[str, object]]:
    records = [
        _mapping(json.loads(line), f"session record {index}")
        for index, line in enumerate(path.read_text().splitlines(), start=1)
        if line.strip()
    ]
    if not records or records[0].get("type") != "session":
        raise EffectivenessError(f"session has no v3 header: {path}")
    by_id: dict[str, dict[str, object]] = {}
    leaf: str | None = None
    for entry in records[1:]:
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            raise EffectivenessError(f"session entry has no id: {path}")
        by_id[entry_id] = entry
        leaf = entry_id
    branch: list[dict[str, object]] = []
    seen: set[str] = set()
    current = leaf
    while current is not None:
        if current in seen or current not in by_id:
            raise EffectivenessError(f"session tree is invalid: {path}")
        seen.add(current)
        entry = by_id[current]
        branch.append(entry)
        parent = entry.get("parentId")
        if parent is not None and not isinstance(parent, str):
            raise EffectivenessError(f"session parent is invalid: {path}")
        current = parent
    branch.reverse()
    return branch


def _aggregate(trials: list[TrialOutcome]) -> dict[str, object]:
    if not trials:
        raise EffectivenessError("cannot aggregate an empty condition")
    return {
        "trials": len(trials),
        "successes": sum(trial.success for trial in trials),
        "unknownRewards": sum(
            trial.terminal_outcome.reward is None for trial in trials
        ),
        "successRate": fmean(1.0 if trial.success else 0.0 for trial in trials),
        "meanToolCalls": fmean(trial.tool_calls for trial in trials),
        "meanTokens": fmean(trial.total_tokens for trial in trials),
        "meanCostUsd": fmean(trial.cost_usd for trial in trials),
        "totalCostUsd": sum(trial.cost_usd for trial in trials),
        "memoryExposureRate": fmean(
            1.0 if trial.memory_item_ids else 0.0 for trial in trials
        ),
    }


def _evaluation_complete(trials: list[TrialOutcome]) -> bool:
    return bool(trials) and all(
        trial.session_jsonl is not None
        and trial.terminal_outcome.reward in (0, 1)
        and trial.terminal_outcome.termination_reason
        in {TerminationReason.SUCCESS, TerminationReason.FAILED_ASSERTION}
        for trial in trials
    )


def _paired_deltas(trials: list[TrialOutcome]) -> dict[str, float]:
    off = [trial for trial in trials if trial.condition == "memory_off"]
    on = [trial for trial in trials if trial.condition == "memory_on"]
    if len(off) != len(on) or not off:
        raise EffectivenessError("paired conditions have unequal trial counts")
    return {
        "successRateOnMinusOff": fmean(1.0 if trial.success else 0.0 for trial in on)
        - fmean(1.0 if trial.success else 0.0 for trial in off),
        "meanToolCallsOnMinusOff": fmean(trial.tool_calls for trial in on)
        - fmean(trial.tool_calls for trial in off),
        "meanTokensOnMinusOff": fmean(trial.total_tokens for trial in on)
        - fmean(trial.total_tokens for trial in off),
        "meanCostUsdOnMinusOff": fmean(trial.cost_usd for trial in on)
        - fmean(trial.cost_usd for trial in off),
    }


def _memory_item_count(path: Path) -> int:
    try:
        with sqlite3.connect(path) as connection:
            row = connection.execute("SELECT COUNT(*) FROM memory_items").fetchone()
    except sqlite3.Error as error:
        raise EffectivenessError(
            f"memory bank schema is unavailable: {error}"
        ) from error
    if row is None or not isinstance(row[0], int) or isinstance(row[0], bool):
        raise EffectivenessError("memory bank returned an invalid item count")
    return row[0]


def _capture_source_snapshot(root: Path, cli_path: Path) -> SourceSnapshot:
    tracked_diff = _command_bytes(
        "git", "-C", str(root), "diff", "--binary", "--full-index", "HEAD", "--"
    )
    listed = _nul_paths(
        _command_bytes(
            "git",
            "-C",
            str(root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        )
    )
    untracked = set(
        _nul_paths(
            _command_bytes(
                "git",
                "-C",
                str(root),
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
            )
        )
    )
    workspace_digest = hashlib.sha256()
    untracked_files: list[tuple[str, str]] = []
    for relative_path in sorted(set(listed)):
        encoded_path = os.fsencode(relative_path)
        payload = _source_path_payload(root / relative_path)
        workspace_digest.update(len(encoded_path).to_bytes(8, "big"))
        workspace_digest.update(encoded_path)
        workspace_digest.update(len(payload).to_bytes(8, "big"))
        workspace_digest.update(payload)
        if relative_path in untracked:
            untracked_files.append((relative_path, hashlib.sha256(payload).hexdigest()))
    return SourceSnapshot(
        commit=_command_output("git", "-C", str(root), "rev-parse", "HEAD"),
        status=tuple(
            _command_output("git", "-C", str(root), "status", "--short").splitlines()
        ),
        workspace_content_sha256=workspace_digest.hexdigest(),
        tracked_diff_sha256=hashlib.sha256(tracked_diff).hexdigest(),
        untracked_files=tuple(untracked_files),
        source_file_count=len(set(listed)),
        runtime_cli_sha256=_file_sha256(cli_path),
    )


def _nul_paths(value: bytes) -> tuple[str, ...]:
    return tuple(os.fsdecode(part) for part in value.split(b"\0") if part)


def _source_path_payload(path: Path) -> bytes:
    if path.is_symlink():
        return b"symlink\0" + os.fsencode(os.readlink(path))
    if path.is_file():
        return b"file\0" + path.read_bytes()
    return b"missing\0"


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise EffectivenessError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EffectivenessError(f"{label} must be a nonempty string")
    return value.strip()


def _nonnegative_integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise EffectivenessError(f"{label} must be a nonnegative integer")
    return value


def _nonnegative_number(value: object, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or float(value) < 0
    ):
        raise EffectivenessError(f"{label} must be a nonnegative number")
    return float(value)


def _optional_integer(value: object) -> int | None:
    if value is None:
        return None
    return _nonnegative_integer(value, "optional integer")


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise EffectivenessError(f"{label} must be a boolean")
    return value


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise EffectivenessError(f"{label} must be an array of strings")
    return tuple(value)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _run(*command: str) -> None:
    completed = subprocess.run(
        command, capture_output=True, text=True, check=False, env=os.environ.copy()
    )
    if completed.returncode != 0:
        raise EffectivenessError(
            completed.stderr.strip()
            or completed.stdout.strip()
            or f"command failed: {' '.join(command)}"
        )


def _command_output(*command: str) -> str:
    completed = subprocess.run(
        command, capture_output=True, text=True, check=False, env=os.environ.copy()
    )
    if completed.returncode != 0:
        raise EffectivenessError(
            completed.stderr.strip()
            or completed.stdout.strip()
            or f"command failed: {' '.join(command)}"
        )
    return completed.stdout.rstrip("\n")


def _command_bytes(*command: str) -> bytes:
    completed = subprocess.run(
        command, capture_output=True, check=False, env=os.environ.copy()
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode(errors="replace").strip()
        stdout = completed.stdout.decode(errors="replace").strip()
        raise EffectivenessError(
            stderr or stdout or f"command failed: {' '.join(command)}"
        )
    return completed.stdout
