"""BATS batch driver.

wired into the synth CLI at Phase 5 integration (orphan-check exemption)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, TypedDict

from middleware.prompts_bats import PR_CHAT_VERIFICATION

VerificationDecision = Literal["SUCCESS", "CONTINUE", "PIVOT"]
VerifierResponse = str | Mapping[str, object]

# Source: budget-aware-agent/src/agent_bats.py:277 at 37d23f6.
# The source starts a fresh trajectory only for remaining budgets > 5 and > 3.
BATS_FRESH_QUERY_THRESHOLD = 5
BATS_FRESH_FILE_THRESHOLD = 3

_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_BUDGET_BLOCK_PATTERN = re.compile(r"<budget>(.*?)</budget>", re.DOTALL)
_NUMBER_PATTERN = r"(?:\d+(?:\.\d+)?|\.\d+)"
_BUDGET_FIELDS = (
    ("bash", "Bash"),
    ("edit", "Edit"),
    ("ipython", "IPython"),
    ("subagents", "Subagent"),
    ("tokens", "Token"),
    ("costUsd", "Cost USD"),
)
_VERIFICATION_KEYS = {
    "verification",
    "decision",
    "justification",
    "trajectory_summary",
    "details",
}


class VerificationPayload(TypedDict):
    verification: str
    decision: VerificationDecision
    justification: str
    trajectory_summary: str
    details: dict[str, object]


@dataclass(frozen=True)
class BudgetCounterState:
    used: float
    remaining: float | None

    @property
    def limit(self) -> float | None:
        return None if self.remaining is None else self.used + self.remaining

    @property
    def remaining_fraction(self) -> float | None:
        remaining = self.remaining
        if remaining is None:
            return None
        limit = self.used + remaining
        return 0.0 if limit == 0 else remaining / limit


@dataclass(frozen=True)
class BudgetState:
    bash: BudgetCounterState
    edit: BudgetCounterState
    ipython: BudgetCounterState
    subagents: BudgetCounterState
    tokens: BudgetCounterState
    cost_usd: BudgetCounterState

    def used_values(self) -> dict[str, float]:
        return {
            "bash": self.bash.used,
            "edit": self.edit.used,
            "ipython": self.ipython.used,
            "subagents": self.subagents.used,
            "tokens": self.tokens.used,
            "costUsd": self.cost_usd.used,
        }

    def bounded_fractions(self) -> tuple[float, ...]:
        fractions = (
            self.bash.remaining_fraction,
            self.edit.remaining_fraction,
            self.ipython.remaining_fraction,
            self.subagents.remaining_fraction,
            self.tokens.remaining_fraction,
            self.cost_usd.remaining_fraction,
        )
        return tuple(value for value in fractions if value is not None)


@dataclass(frozen=True)
class BatchTaskSpec:
    target_repo: Path
    prompt: str
    max_trajectories: int
    base_ref: str = "HEAD"


@dataclass(frozen=True)
class BatchDriverConfig:
    cli_argv: tuple[str, ...]
    run_id: str
    cli_config_args: tuple[str, ...] = ()
    timeout_seconds: float = 900.0
    env: Mapping[str, str] = field(default_factory=dict)
    continue_generation: bool = True
    min_budget_fraction: float = 0.2
    private_daemon_cli: Path | None = None
    acceptance_argv: tuple[str, ...] = ()
    acceptance_timeout_seconds: float = 120.0
    acceptance_inputs: Mapping[Path, str] = field(default_factory=dict)
    immutable_worktree_paths: Mapping[str, str] = field(default_factory=dict)
    memory_db_seed: Path | None = None
    telemetry_relative_dir: Path = Path(".synth/telemetry")


@dataclass(frozen=True)
class TrajectoryResult:
    index: int
    exit_code: int
    stdout: str
    stderr: str
    session_jsonl: Path | None
    budget: BudgetState | None
    diff: str
    timed_out: bool
    acceptance_exit_code: int | None
    acceptance_stdout: str
    acceptance_stderr: str
    acceptance_timed_out: bool
    acceptance_integrity_failures: tuple[str, ...]
    telemetry_files: tuple[Path, ...]


@dataclass(frozen=True)
class VerificationRequest:
    task: BatchTaskSpec
    trajectory: TrajectoryResult
    verifier_prompt: str = PR_CHAT_VERIFICATION


class Verifier(Protocol):
    def __call__(self, request: VerificationRequest, /) -> VerifierResponse: ...


@dataclass(frozen=True)
class BatchRunResult:
    run_dir: Path
    trajectories_run: int
    candidates_recorded: int
    stop_reason: str


class BatchDriverError(RuntimeError):
    pass


def parse_verification(payload: VerifierResponse) -> VerificationPayload:
    """Parse the strict ``pr_chat_verification`` JSON contract."""
    decoded: object = json.loads(payload) if isinstance(payload, str) else payload
    if not isinstance(decoded, Mapping):
        raise ValueError("verification result must be a JSON object")
    if set(decoded) != _VERIFICATION_KEYS:
        raise ValueError(
            "verification result must contain exactly: "
            + ", ".join(sorted(_VERIFICATION_KEYS))
        )

    verification = decoded["verification"]
    decision = decoded["decision"]
    justification = decoded["justification"]
    trajectory_summary = decoded["trajectory_summary"]
    details = decoded["details"]
    if not all(
        isinstance(value, str)
        for value in (verification, decision, justification, trajectory_summary)
    ):
        raise ValueError("verification text fields and decision must be strings")
    if decision not in ("SUCCESS", "CONTINUE", "PIVOT"):
        raise ValueError(f"unknown verification decision: {decision}")
    if not isinstance(details, Mapping) or not all(
        isinstance(key, str) for key in details
    ):
        raise ValueError("verification details must be a JSON object")

    return {
        "verification": verification,
        "decision": decision,
        "justification": justification,
        "trajectory_summary": trajectory_summary,
        "details": dict(details),
    }


def parse_budget_text(text: str) -> BudgetState | None:
    """Return the last complete persisted Synth budget block."""
    states: list[BudgetState] = []
    for match in _BUDGET_BLOCK_PATTERN.finditer(text):
        counters: dict[str, BudgetCounterState] = {}
        block = match.group(1)
        for budget_field, label in _BUDGET_FIELDS:
            field_match = re.search(
                rf"{re.escape(label)} Budget Used:\s*({_NUMBER_PATTERN}),\s*"
                rf"{re.escape(label)} Budget Remaining:\s*"
                rf"({_NUMBER_PATTERN}|unlimited)",
                block,
            )
            if field_match is None:
                break
            remaining_text = field_match.group(2)
            counters[budget_field] = BudgetCounterState(
                used=float(field_match.group(1)),
                remaining=(
                    None if remaining_text == "unlimited" else float(remaining_text)
                ),
            )
        if len(counters) != len(_BUDGET_FIELDS):
            continue
        states.append(
            BudgetState(
                bash=counters["bash"],
                edit=counters["edit"],
                ipython=counters["ipython"],
                subagents=counters["subagents"],
                tokens=counters["tokens"],
                cost_usd=counters["costUsd"],
            )
        )
    return states[-1] if states else None


def should_start_fresh_trajectory(
    budget: BudgetState, min_budget_fraction: float = 0.2
) -> bool:
    """Require every bounded Synth budget to remain strictly above the threshold."""
    if not 0.0 <= min_budget_fraction <= 1.0:
        raise ValueError("min_budget_fraction must be between zero and one")
    return all(
        fraction > min_budget_fraction for fraction in budget.bounded_fractions()
    )


def run_batch(
    task: BatchTaskSpec,
    config: BatchDriverConfig,
    verifier: Verifier,
) -> BatchRunResult:
    """Run success-scaled print sessions and persist their durable artifacts."""
    repo = _validate_inputs(task, config)
    base_commit = _git(repo, "rev-parse", "--verify", f"{task.base_ref}^{{commit}}")
    run_dir = repo / ".synth" / "runs" / config.run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    trajectories_dir = run_dir / "trajectories"
    candidates_dir = run_dir / "candidates"
    trajectories_dir.mkdir()
    candidates_dir.mkdir()

    worktree_parent = Path(tempfile.mkdtemp(prefix="synth-batch-"))
    candidates_recorded = 0
    trajectories_run = 0
    stop_reason = "trajectory_limit"
    carried_budget_used: Mapping[str, float] = {}
    try:
        for index in range(1, task.max_trajectories + 1):
            trajectories_run = index
            trajectory_dir = trajectories_dir / str(index)
            trajectory_dir.mkdir()
            worktree = worktree_parent / f"trajectory-{index}"
            _git(repo, "worktree", "add", "--detach", str(worktree), base_commit)
            try:
                result = _invoke_trajectory(
                    task=task,
                    config=config,
                    index=index,
                    worktree=worktree,
                    trajectory_dir=trajectory_dir,
                    initial_budget_used=carried_budget_used,
                )
                verification: VerificationPayload | None = None
                candidate_number: int | None = None

                if result.exit_code != 0:
                    action = "stop_cli_failure"
                    stop_reason = "cli_timeout" if result.timed_out else "cli_failure"
                elif result.acceptance_timed_out:
                    action = "stop_acceptance_timeout"
                    stop_reason = "acceptance_timeout"
                elif result.acceptance_integrity_failures:
                    action = "stop_acceptance_integrity_failure"
                    stop_reason = "acceptance_integrity_failure"
                elif (
                    result.acceptance_exit_code is not None
                    and result.acceptance_exit_code != 0
                ):
                    action = "stop_acceptance_failure"
                    stop_reason = "acceptance_failure"
                elif result.session_jsonl is None:
                    action = "stop_missing_session"
                    stop_reason = "missing_session"
                else:
                    verification = parse_verification(
                        verifier(VerificationRequest(task=task, trajectory=result))
                    )
                    decision = verification["decision"]
                    if decision == "SUCCESS":
                        candidates_recorded += 1
                        candidate_number = candidates_recorded
                        _record_candidate(
                            candidates_dir=candidates_dir,
                            candidate_number=candidate_number,
                            trajectory=result,
                            verification=verification,
                        )
                        if result.budget is None:
                            action = "stop_missing_budget"
                            stop_reason = "missing_budget"
                        elif (
                            config.continue_generation
                            and should_start_fresh_trajectory(
                                result.budget, config.min_budget_fraction
                            )
                        ):
                            carried_budget_used = result.budget.used_values()
                            action = "start_fresh_trajectory"
                            stop_reason = "trajectory_limit"
                        elif not config.continue_generation:
                            action = "stop_continuation_disabled"
                            stop_reason = "continuation_disabled"
                        else:
                            action = "stop_low_budget"
                            stop_reason = "low_budget"
                    elif decision == "CONTINUE":
                        action = "continue_in_session"
                        stop_reason = "continue_in_session"
                    else:
                        action = "pivot_in_session"
                        stop_reason = "pivot_in_session"

                _write_trajectory_metadata(
                    trajectory_dir=trajectory_dir,
                    run_dir=run_dir,
                    result=result,
                    verification=verification,
                    candidate_number=candidate_number,
                    action=action,
                )
                if action != "start_fresh_trajectory":
                    break
            finally:
                _git(repo, "worktree", "remove", "--force", str(worktree))
    finally:
        worktree_parent.rmdir()

    _write_json(
        run_dir / "run.json",
        {
            "run_id": config.run_id,
            "target_repo": str(repo),
            "base_commit": base_commit,
            "prompt": task.prompt,
            "max_trajectories": task.max_trajectories,
            "trajectories_run": trajectories_run,
            "candidates_recorded": candidates_recorded,
            "stop_reason": stop_reason,
            "continue_generation": config.continue_generation,
            "min_budget_fraction": config.min_budget_fraction,
        },
    )
    return BatchRunResult(
        run_dir=run_dir,
        trajectories_run=trajectories_run,
        candidates_recorded=candidates_recorded,
        stop_reason=stop_reason,
    )


def _validate_inputs(task: BatchTaskSpec, config: BatchDriverConfig) -> Path:
    if task.max_trajectories < 1:
        raise ValueError("max_trajectories must be positive")
    if not task.prompt.strip():
        raise ValueError("task prompt must not be empty")
    if not config.cli_argv:
        raise ValueError("cli_argv must not be empty")
    if config.timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if config.acceptance_timeout_seconds <= 0:
        raise ValueError("acceptance_timeout_seconds must be positive")
    if (config.acceptance_inputs or config.immutable_worktree_paths) and not (
        config.acceptance_argv
    ):
        raise ValueError("acceptance integrity inputs require acceptance_argv")
    if not 0.0 <= config.min_budget_fraction <= 1.0:
        raise ValueError("min_budget_fraction must be between zero and one")
    if _RUN_ID_PATTERN.fullmatch(config.run_id) is None:
        raise ValueError("run_id contains unsafe path characters")
    if (
        config.private_daemon_cli is not None
        and not config.private_daemon_cli.is_file()
    ):
        raise ValueError(
            f"private daemon CLI is not a file: {config.private_daemon_cli}"
        )
    if config.memory_db_seed is not None and not config.memory_db_seed.is_file():
        raise ValueError(f"memory DB seed is not a file: {config.memory_db_seed}")
    for path, expected_digest in config.acceptance_inputs.items():
        if not path.is_absolute():
            raise ValueError(f"acceptance input must be absolute: {path}")
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"acceptance input must be a regular file: {path}")
        _validate_expected_digest(expected_digest, f"acceptance input {path}")
        if _file_sha256(path) != expected_digest:
            raise ValueError(f"acceptance input digest does not match: {path}")
    for relative_path, expected_digest in config.immutable_worktree_paths.items():
        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError(
                f"immutable worktree path must be a safe relative path: {relative_path}"
            )
        _validate_expected_digest(
            expected_digest, f"immutable worktree path {relative_path}"
        )
    if (
        config.telemetry_relative_dir.is_absolute()
        or ".." in config.telemetry_relative_dir.parts
        or not config.telemetry_relative_dir.parts
    ):
        raise ValueError("telemetry_relative_dir must be a safe relative path")

    requested_repo = task.target_repo.resolve(strict=True)
    actual_repo = Path(_git(requested_repo, "rev-parse", "--show-toplevel")).resolve()
    if requested_repo != actual_repo:
        raise ValueError("target_repo must be the repository root")
    return actual_repo


def _invoke_trajectory(
    *,
    task: BatchTaskSpec,
    config: BatchDriverConfig,
    index: int,
    worktree: Path,
    trajectory_dir: Path,
    initial_budget_used: Mapping[str, float],
) -> TrajectoryResult:
    if config.memory_db_seed is not None:
        memory_path = worktree / ".synth" / "synth.db"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(config.memory_db_seed, memory_path)

    baseline_failures = _acceptance_integrity_failures(config, worktree)
    if baseline_failures:
        raise BatchDriverError(
            "acceptance inputs did not match their baseline before the trajectory: "
            + ", ".join(baseline_failures)
        )

    session_dir = trajectory_dir / "session"
    session_dir.mkdir()
    daemon_socket = worktree.parent / f"daemon-{index}.sock"
    command = [
        *config.cli_argv,
        *config.cli_config_args,
        *(
            ("--daemon-socket", str(daemon_socket))
            if config.private_daemon_cli is not None
            else ()
        ),
        "--cwd",
        str(worktree),
        "--session-dir",
        str(session_dir),
        "--print",
        task.prompt,
    ]
    env = os.environ.copy()
    env.update(config.env)
    env["SYNTH_BATCH_RUN_ID"] = config.run_id
    env["SYNTH_BATCH_TRAJECTORY_INDEX"] = str(index)
    env["SYNTH_BATCH_FORCE_VERIFIER"] = "1"
    env["DO_NOT_TRACK"] = "1"
    env["PRIME_AGENT_TELEMETRY"] = "0"
    if initial_budget_used:
        env["SYNTH_BATCH_BUDGET_USED_JSON"] = json.dumps(
            initial_budget_used, separators=(",", ":"), sort_keys=True
        )

    timed_out = False
    try:
        try:
            completed = subprocess.run(
                command,
                cwd=worktree,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=config.timeout_seconds,
            )
            exit_code = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as error:
            timed_out = True
            exit_code = 124
            stdout = _timeout_output(error.stdout)
            stderr = _timeout_output(error.stderr)
    finally:
        if config.private_daemon_cli is not None:
            _shutdown_private_daemon(
                node_binary=config.cli_argv[0],
                cli_path=config.private_daemon_cli,
                socket_path=daemon_socket,
                cwd=worktree,
                environment=env,
            )

    acceptance_exit_code: int | None = None
    acceptance_stdout = ""
    acceptance_stderr = ""
    acceptance_timed_out = False
    acceptance_integrity_failures = (
        _acceptance_integrity_failures(config, worktree)
        if config.acceptance_argv and not timed_out and exit_code == 0
        else ()
    )
    if acceptance_integrity_failures:
        acceptance_exit_code = 125
        acceptance_stderr = (
            "acceptance integrity failure: "
            + ", ".join(acceptance_integrity_failures)
            + "\n"
        )
    elif config.acceptance_argv and not timed_out and exit_code == 0:
        acceptance_env = env.copy()
        acceptance_env["SYNTH_ACCEPTANCE_WORKTREE"] = str(worktree)
        try:
            acceptance = subprocess.run(
                config.acceptance_argv,
                cwd=worktree,
                env=acceptance_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=config.acceptance_timeout_seconds,
            )
            acceptance_exit_code = acceptance.returncode
            acceptance_stdout = acceptance.stdout
            acceptance_stderr = acceptance.stderr
        except subprocess.TimeoutExpired as error:
            acceptance_timed_out = True
            acceptance_exit_code = 124
            acceptance_stdout = _timeout_output(error.stdout)
            acceptance_stderr = _timeout_output(error.stderr)

    (trajectory_dir / "stdout.txt").write_text(stdout)
    (trajectory_dir / "stderr.txt").write_text(stderr)
    (trajectory_dir / "acceptance-stdout.txt").write_text(acceptance_stdout)
    (trajectory_dir / "acceptance-stderr.txt").write_text(acceptance_stderr)
    diff = _capture_diff(worktree)
    (trajectory_dir / "diff.patch").write_text(diff)
    telemetry_files = _preserve_telemetry(
        worktree / config.telemetry_relative_dir,
        trajectory_dir / "telemetry",
    )
    session_jsonl = _find_session_jsonl(session_dir)
    budget = (
        parse_budget_text(session_jsonl.read_text())
        if session_jsonl is not None
        else None
    )
    return TrajectoryResult(
        index=index,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        session_jsonl=session_jsonl,
        budget=budget,
        diff=diff,
        timed_out=timed_out,
        acceptance_exit_code=acceptance_exit_code,
        acceptance_stdout=acceptance_stdout,
        acceptance_stderr=acceptance_stderr,
        acceptance_timed_out=acceptance_timed_out,
        acceptance_integrity_failures=acceptance_integrity_failures,
        telemetry_files=telemetry_files,
    )


def _acceptance_integrity_failures(
    config: BatchDriverConfig, worktree: Path
) -> tuple[str, ...]:
    failures: list[str] = []
    for path, expected_digest in config.acceptance_inputs.items():
        if (
            path.is_symlink()
            or not path.is_file()
            or _file_sha256(path) != expected_digest
        ):
            failures.append(str(path))
    for relative_path, expected_digest in config.immutable_worktree_paths.items():
        path = worktree / relative_path
        if (
            path.is_symlink()
            or not path.is_file()
            or _file_sha256(path) != expected_digest
        ):
            failures.append(relative_path)
    return tuple(failures)


def _validate_expected_digest(digest: str, label: str) -> None:
    if _SHA256_PATTERN.fullmatch(digest) is None:
        raise ValueError(f"{label} must have a lowercase SHA-256 digest")


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _preserve_telemetry(source: Path, destination: Path) -> tuple[Path, ...]:
    if not source.is_dir():
        return ()
    destination.mkdir()
    preserved: list[Path] = []
    for path in sorted(source.rglob("*.jsonl")):
        if path.is_symlink() or not path.is_file():
            continue
        relative_path = path.relative_to(source)
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        preserved.append(target)
    return tuple(preserved)


def _find_session_jsonl(session_dir: Path) -> Path | None:
    sessions = sorted(session_dir.rglob("*.jsonl"))
    if not sessions:
        return None
    if len(sessions) == 1:
        return sessions[0]

    roots: list[Path] = []
    for session in sessions:
        first_line = session.read_text().splitlines()[0]
        decoded: object = json.loads(first_line)
        if (
            isinstance(decoded, Mapping)
            and decoded.get("type") == "session"
            and decoded.get("parentSession") is None
        ):
            roots.append(session)
    if len(roots) != 1:
        raise BatchDriverError(
            f"expected one root session JSONL, found {len(roots)} in {session_dir}"
        )
    return roots[0]


def _capture_diff(worktree: Path) -> str:
    tracked = _git(worktree, "diff", "--binary", "--full-index", "HEAD", "--")
    untracked_output = _git(
        worktree, "ls-files", "--others", "--exclude-standard", "-z"
    )
    patches = [tracked] if tracked else []
    for relative_path in filter(None, untracked_output.split("\0")):
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "diff",
                "--binary",
                "--full-index",
                "--no-index",
                "--",
                "/dev/null",
                relative_path,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode not in (0, 1):
            raise BatchDriverError(
                completed.stderr.strip() or "failed to diff new file"
            )
        if completed.stdout:
            patches.append(completed.stdout.rstrip("\n"))
    return "\n".join(patches) + ("\n" if patches else "")


def _record_candidate(
    *,
    candidates_dir: Path,
    candidate_number: int,
    trajectory: TrajectoryResult,
    verification: VerificationPayload,
) -> None:
    candidate_dir = candidates_dir / str(candidate_number)
    candidate_dir.mkdir()
    (candidate_dir / "diff.patch").write_text(trajectory.diff)
    _write_json(candidate_dir / "verification.json", verification)
    _write_json(
        candidate_dir / "candidate.json",
        {
            "candidate": candidate_number,
            "trajectory": trajectory.index,
            "session_jsonl": str(trajectory.session_jsonl),
            "exit_code": trajectory.exit_code,
            "acceptance_exit_code": trajectory.acceptance_exit_code,
            "acceptance_timed_out": trajectory.acceptance_timed_out,
        },
    )


def _write_trajectory_metadata(
    *,
    trajectory_dir: Path,
    run_dir: Path,
    result: TrajectoryResult,
    verification: VerificationPayload | None,
    candidate_number: int | None,
    action: str,
) -> None:
    session_path = (
        str(result.session_jsonl.relative_to(run_dir))
        if result.session_jsonl is not None
        else None
    )
    budget = (
        {
            "bash": _counter_json(result.budget.bash),
            "edit": _counter_json(result.budget.edit),
            "ipython": _counter_json(result.budget.ipython),
            "subagents": _counter_json(result.budget.subagents),
            "tokens": _counter_json(result.budget.tokens),
            "costUsd": _counter_json(result.budget.cost_usd),
        }
        if result.budget is not None
        else None
    )
    _write_json(
        trajectory_dir / "trajectory.json",
        {
            "trajectory": result.index,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "acceptance_exit_code": result.acceptance_exit_code,
            "acceptance_timed_out": result.acceptance_timed_out,
            "acceptance_integrity_failures": list(result.acceptance_integrity_failures),
            "acceptance_stdout": "acceptance-stdout.txt",
            "acceptance_stderr": "acceptance-stderr.txt",
            "stdout": "stdout.txt",
            "stderr": "stderr.txt",
            "diff": "diff.patch",
            "session_jsonl": session_path,
            "budget": budget,
            "verification": verification,
            "candidate": candidate_number,
            "next_action": action,
            "telemetry": [
                str(path.relative_to(trajectory_dir)) for path in result.telemetry_files
            ],
        },
    )


def _counter_json(counter: BudgetCounterState) -> dict[str, float | None]:
    return {"used": counter.used, "remaining": counter.remaining}


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise BatchDriverError(completed.stderr.strip() or "git command failed")
    return completed.stdout.rstrip("\n")


def _shutdown_private_daemon(
    *,
    node_binary: str,
    cli_path: Path,
    socket_path: Path,
    cwd: Path,
    environment: Mapping[str, str],
) -> None:
    if not socket_path.exists():
        return
    module_path = cli_path.parent / "cli" / "daemon-launch.js"
    if not module_path.is_file():
        raise BatchDriverError(f"daemon shutdown module is missing: {module_path}")
    script = (
        "import { shutdownDaemonAndWait } from "
        f"{json.dumps(module_path.as_uri())};"
        "if (!(await shutdownDaemonAndWait(process.argv[1], 10000))) process.exit(1);"
    )
    try:
        completed = subprocess.run(
            [
                node_binary,
                "--input-type=module",
                "--eval",
                script,
                str(socket_path),
            ],
            cwd=cwd,
            env=dict(environment),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired as error:
        raise BatchDriverError("private daemon shutdown timed out") from error
    if completed.returncode != 0:
        raise BatchDriverError("private daemon shutdown failed")


def _timeout_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value
