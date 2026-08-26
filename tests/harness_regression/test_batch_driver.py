from __future__ import annotations

import hashlib
import json
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
    assert metadata["acceptance_exit_code"] == 0
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
    assert metadata["acceptance_exit_code"] == 125
    assert metadata["acceptance_integrity_failures"] == ["test_acceptance.py"]
    assert metadata["next_action"] == "stop_acceptance_integrity_failure"
    assert (
        "acceptance integrity failure"
        in (trajectory_dir / "acceptance-stderr.txt").read_text()
    )
    assert not acceptance_marker.exists()
    assert not any((result.run_dir / "candidates").iterdir())
