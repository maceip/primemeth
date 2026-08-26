from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from middleware.batch_driver import (
    BatchDriverConfig,
    BatchTaskSpec,
    BudgetCounterState,
    BudgetState,
    VerificationRequest,
    parse_budget_text,
    parse_verification,
    run_batch,
    should_start_fresh_trajectory,
)
from middleware.telemetry import TelemetryEvent, validate_telemetry_file
from middleware.terminal_outcome import (
    OutcomeEvidence,
    OutcomeSignals,
    TerminalOutcomeReceipt,
    TerminationReason,
    classify_terminal_outcome,
    read_terminal_outcome_receipt,
)

_ROOT = Path(__file__).resolve().parents[2]
_FAKE_CLI = _ROOT / "tests" / "batch_fixtures" / "fake_coding_agent.py"


def _budget(remaining: float | None) -> BudgetState:
    counter = BudgetCounterState(used=1.0, remaining=remaining)
    unlimited = BudgetCounterState(used=0.0, remaining=None)
    return BudgetState(
        bash=counter,
        edit=unlimited,
        ipython=unlimited,
        subagents=unlimited,
        tokens=unlimited,
        cost_usd=unlimited,
    )


@pytest.mark.parametrize(
    ("remaining", "threshold", "expected"),
    [(4.1, 0.8, True), (4.0, 0.8, False), (0.0, 0.0, False), (None, 1.0, True)],
)
def test_fresh_trajectory_thresholds_are_strict(
    remaining: float | None, threshold: float, expected: bool
) -> None:
    assert should_start_fresh_trajectory(_budget(remaining), threshold) is expected


def test_parse_budget_text_uses_last_persisted_block() -> None:
    text = (
        "<budget>\n"
        "Bash Budget Used: 4, Bash Budget Remaining: 6, "
        "Edit Budget Used: 5, Edit Budget Remaining: 4, "
        "IPython Budget Used: 1, IPython Budget Remaining: unlimited, "
        "Subagent Budget Used: 2, Subagent Budget Remaining: 2, "
        "Token Budget Used: 30, Token Budget Remaining: unlimited, "
        "Cost USD Budget Used: 0.25, Cost USD Budget Remaining: 0.75\n"
        "Make the best use of the available resources.\n"
        "</budget>\n"
    )
    parsed = parse_budget_text(text)
    assert parsed is not None
    assert parsed.used_values() == {
        "bash": 4.0,
        "edit": 5.0,
        "ipython": 1.0,
        "subagents": 2.0,
        "tokens": 30.0,
        "costUsd": 0.25,
    }
    assert parsed.bounded_fractions() == (0.6, 4 / 9, 0.5, 0.75)


@pytest.mark.parametrize("decision", ["SUCCESS", "CONTINUE", "PIVOT"])
def test_parse_verification_accepts_contract_decisions(decision: str) -> None:
    parsed = parse_verification(
        {
            "verification": "checked",
            "decision": decision,
            "justification": "fixture",
            "trajectory_summary": "summary",
            "details": {},
        }
    )
    assert parsed["decision"] == decision


def test_parse_verification_rejects_generation_without_verdict() -> None:
    with pytest.raises(ValueError, match="exactly"):
        parse_verification({"answer": "looks good"})


def test_batch_preserves_acceptance_memory_seed_and_telemetry(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "target"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text(".synth/\n")
    subprocess.run(["git", "-C", str(repo), "add", ".gitignore"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Synth Fixture",
            "-c",
            "user.email=synth@example.invalid",
            "commit",
            "-q",
            "-m",
            "base",
        ],
        check=True,
    )
    seed = tmp_path / "seed.db"
    seed.write_text("immutable-memory-seed")

    def success_verifier(request: VerificationRequest) -> dict[str, object]:
        assert request.trajectory.acceptance_exit_code == 0
        return {
            "verification": "fixture acceptance passed",
            "decision": "SUCCESS",
            "justification": "deterministic fixture",
            "trajectory_summary": "the fake agent wrote its solution",
            "details": {},
        }

    result = run_batch(
        BatchTaskSpec(
            target_repo=repo,
            prompt="Implement the fixture change",
            max_trajectories=1,
        ),
        BatchDriverConfig(
            cli_argv=(sys.executable, str(_FAKE_CLI)),
            run_id="acceptance-fixture",
            env={
                "SYNTH_FAKE_MEMORY_SEED": seed.read_text(),
                "SYNTH_FAKE_WRITE_TELEMETRY": "1",
            },
            continue_generation=False,
            memory_db_seed=seed,
            acceptance_argv=(
                sys.executable,
                "-c",
                "from pathlib import Path; assert Path('solution.txt').is_file()",
            ),
        ),
        success_verifier,
    )

    trajectory_dir = result.run_dir / "trajectories" / "1"
    metadata = json.loads((trajectory_dir / "trajectory.json").read_text())
    assert result.candidates_recorded == 1
    assert result.terminal_outcome.reward == 1
    assert (
        result.terminal_outcome.termination_reason is TerminationReason.SUCCESS
    )
    assert metadata["acceptance_exit_code"] == 0
    assert metadata["terminal_outcome"] == result.terminal_outcome.as_dict()
    receipt = read_terminal_outcome_receipt(
        trajectory_dir / "terminal-outcome.json"
    )
    assert receipt.outcome == result.terminal_outcome
    assert receipt.session_id == "fake-session-1"
    assert metadata["next_action"] == "stop_continuation_disabled"
    assert metadata["telemetry"] == ["telemetry/fake.jsonl"]
    assert (trajectory_dir / "telemetry" / "fake.jsonl").is_file()
    assert (trajectory_dir / "session" / "session-1.jsonl").is_file()


def test_acceptance_failure_skips_verifier_and_candidate(tmp_path: Path) -> None:
    repo = tmp_path / "target"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text(".synth/\n")
    subprocess.run(["git", "-C", str(repo), "add", ".gitignore"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Synth Fixture",
            "-c",
            "user.email=synth@example.invalid",
            "commit",
            "-q",
            "-m",
            "base",
        ],
        check=True,
    )

    def unexpected_verifier(_request: VerificationRequest) -> dict[str, object]:
        raise AssertionError("verifier must not run after deterministic failure")

    result = run_batch(
        BatchTaskSpec(
            target_repo=repo,
            prompt="Implement the fixture change",
            max_trajectories=1,
        ),
        BatchDriverConfig(
            cli_argv=(sys.executable, str(_FAKE_CLI)),
            run_id="acceptance-failure",
            acceptance_argv=(sys.executable, "-c", "raise SystemExit(7)"),
        ),
        unexpected_verifier,
    )

    metadata = json.loads(
        (result.run_dir / "trajectories" / "1" / "trajectory.json").read_text()
    )
    assert result.candidates_recorded == 0
    assert result.stop_reason == "acceptance_failure"
    assert result.terminal_outcome.reward == 0
    assert (
        result.terminal_outcome.termination_reason
        is TerminationReason.FAILED_ASSERTION
    )
    assert metadata["acceptance_exit_code"] == 7
    assert metadata["next_action"] == "stop_acceptance_failure"
    assert not any((result.run_dir / "candidates").iterdir())


def test_mutated_acceptance_input_is_rejected_before_pytest(tmp_path: Path) -> None:
    repo = tmp_path / "target"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text(".synth/\n")
    acceptance_test = repo / "test_acceptance.py"
    acceptance_test.write_text("def test_real_behavior() -> None:\n    assert False\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", ".gitignore", "test_acceptance.py"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Synth Fixture",
            "-c",
            "user.email=synth@example.invalid",
            "commit",
            "-q",
            "-m",
            "base",
        ],
        check=True,
    )
    expected_digest = hashlib.sha256(acceptance_test.read_bytes()).hexdigest()
    acceptance_marker = tmp_path / "acceptance-ran"

    def unexpected_verifier(_request: VerificationRequest) -> dict[str, object]:
        raise AssertionError("verifier must not run after an integrity failure")

    result = run_batch(
        BatchTaskSpec(
            target_repo=repo,
            prompt="Forge the acceptance input",
            max_trajectories=1,
        ),
        BatchDriverConfig(
            cli_argv=(sys.executable, str(_FAKE_CLI)),
            run_id="acceptance-integrity",
            env={
                "SYNTH_FAKE_MUTATE_PATH": "test_acceptance.py",
                "SYNTH_FAKE_MUTATE_CONTENT": (
                    "def test_real_behavior() -> None:\n    assert True\n"
                ),
            },
            acceptance_argv=(
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    f"Path({str(acceptance_marker)!r}).write_text('yes')"
                ),
            ),
            immutable_worktree_paths={"test_acceptance.py": expected_digest},
        ),
        unexpected_verifier,
    )

    trajectory_dir = result.run_dir / "trajectories" / "1"
    metadata = json.loads((trajectory_dir / "trajectory.json").read_text())
    assert result.candidates_recorded == 0
    assert result.stop_reason == "acceptance_integrity_failure"
    assert result.terminal_outcome.reward is None
    assert (
        result.terminal_outcome.termination_reason is TerminationReason.UNKNOWN
    )
    assert metadata["acceptance_exit_code"] == 125
    assert metadata["acceptance_integrity_failures"] == ["test_acceptance.py"]
    assert metadata["next_action"] == "stop_acceptance_integrity_failure"
    assert (
        "acceptance integrity failure"
        in (trajectory_dir / "acceptance-stderr.txt").read_text()
    )
    assert not acceptance_marker.exists()
    assert not any((result.run_dir / "candidates").iterdir())


def test_timeout_classification_is_deterministic() -> None:
    evidence = OutcomeEvidence("artifact", "/tmp/timeout.json")
    outcome = classify_terminal_outcome(
        OutcomeSignals(
            agent_exit_code=124,
            agent_timed_out=True,
            acceptance_configured=False,
            acceptance_exit_code=None,
            acceptance_timed_out=False,
        ),
        evidence,
    )
    assert outcome.reward == 0
    assert outcome.termination_reason is TerminationReason.TIMEOUT
    receipt = TerminalOutcomeReceipt(
        run_id="timeout-fixture",
        trajectory_index=1,
        session_id=None,
        outcome=outcome,
        signals=OutcomeSignals(
            agent_exit_code=124,
            agent_timed_out=True,
            acceptance_configured=False,
            acceptance_exit_code=None,
            acceptance_timed_out=False,
        ),
    )
    assert TerminalOutcomeReceipt.from_mapping(receipt.as_dict()) == receipt


def test_private_daemon_commits_acceptance_outcome_to_extension_telemetry(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    assert node is not None
    cli_path = _ROOT / "packages" / "coding-agent" / "dist" / "cli.js"
    faux_extension = (
        _ROOT
        / "packages"
        / "coding-agent"
        / "test"
        / "fixtures"
        / "eng-4600-faux-extension.ts"
    )
    assert cli_path.is_file()
    assert faux_extension.is_file()

    repo = tmp_path / "real-target"
    shutil.copytree(
        _ROOT / ".prime" / "agent" / "extensions" / "synth",
        repo / ".prime" / "agent" / "extensions" / "synth",
    )
    (repo / "node_modules").symlink_to(_ROOT / "node_modules", target_is_directory=True)
    (repo / ".gitignore").write_text(".synth/\n")
    (repo / "synth.json").write_text(
        json.dumps(
            {
                "verifier": {
                    "interactive": False,
                    "batch": False,
                    "maxCycles": 0,
                    "contGen": {"enabled": False, "minBudgetFrac": 0.2},
                }
            }
        )
    )
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "add",
            "-f",
            ".gitignore",
            "synth.json",
            ".prime",
            "node_modules",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Synth Fixture",
            "-c",
            "user.email=synth@example.invalid",
            "commit",
            "-q",
            "-m",
            "base",
        ],
        check=True,
    )

    def success_verifier(_request: VerificationRequest) -> dict[str, object]:
        return {
            "verification": "immutable acceptance passed",
            "decision": "SUCCESS",
            "justification": "fixture",
            "trajectory_summary": "faux print session completed",
            "details": {},
        }

    result = run_batch(
        BatchTaskSpec(
            target_repo=repo,
            prompt="Return the faux response",
            max_trajectories=1,
        ),
        BatchDriverConfig(
            cli_argv=(node, str(cli_path)),
            cli_config_args=(
                "--offline",
                "--model",
                "faux/faux",
                "--extension",
                str(faux_extension),
                "--no-tools",
                "--no-skills",
                "--no-prompt-templates",
                "--no-themes",
                "--no-context-files",
            ),
            run_id="terminal-handshake",
            timeout_seconds=60,
            env={
                "PRIME_AGENT_CODING_AGENT_DIR": str(tmp_path / "agent-dir"),
                "PI_OFFLINE": "1",
                "PI_SKIP_VERSION_CHECK": "1",
            },
            continue_generation=False,
            private_daemon_cli=cli_path,
            acceptance_argv=(sys.executable, "-c", "raise SystemExit(0)"),
        ),
        success_verifier,
    )

    trajectory_dir = result.run_dir / "trajectories" / "1"
    telemetry_files = tuple((trajectory_dir / "telemetry").glob("*.jsonl"))
    assert len(telemetry_files) == 1
    assert validate_telemetry_file(telemetry_files[0]).events > 0
    events = [
        TelemetryEvent.from_json_line(line)
        for line in telemetry_files[0].read_text().splitlines()
        if line.strip()
    ]
    terminal_events = [event for event in events if event.kind == "session.outcome"]
    assert len(terminal_events) == 1
    assert terminal_events[0].outcome == result.terminal_outcome
    assert terminal_events[0].outcome is not None
    assert terminal_events[0].outcome.reward == 1
    assert (
        terminal_events[0].outcome.termination_reason
        is TerminationReason.SUCCESS
    )
