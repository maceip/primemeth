"""Runnable three-trajectory, two-candidate BATS batch demonstration."""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

from middleware.batch_driver import (
    BatchDriverConfig,
    BatchTaskSpec,
    VerificationRequest,
    VerifierResponse,
    run_batch,
)
from middleware.batch_select import JudgeRequest, JudgeResult, select_candidates

FAKE_CLI = Path(__file__).with_name("fake_coding_agent.py")


class ScriptedVerifier:
    def __init__(self, decisions: tuple[str, ...]) -> None:
        self._decisions: Iterator[str] = iter(decisions)

    def __call__(self, request: VerificationRequest, /) -> VerifierResponse:
        decision = next(self._decisions)
        return {
            "verification": f"fixture verified trajectory {request.trajectory.index}",
            "decision": decision,
            "justification": "scripted fixture decision",
            "trajectory_summary": request.trajectory.stdout.strip(),
            "details": {},
        }


def fixture_judge(request: JudgeRequest, /) -> JudgeResult:
    if len(request.candidates) != 2:
        raise ValueError("demo judge expected exactly two verified candidates")
    return JudgeResult(
        scores=(0.0, 1.0),
        selected_index=1,
        response="fixture judge selected the second verified diff",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo-root", type=Path, required=True)
    args = parser.parse_args()

    args.demo_root.mkdir(parents=True, exist_ok=False)
    target_repo = args.demo_root / "target"
    target_repo.mkdir()
    _run("git", "init", "-q", str(target_repo))
    _run("git", "-C", str(target_repo), "config", "user.name", "Synth Fixture")
    _run(
        "git",
        "-C",
        str(target_repo),
        "config",
        "user.email",
        "synth-fixture@example.invalid",
    )
    (target_repo / "README.md").write_text("# Batch fixture\n")
    _run("git", "-C", str(target_repo), "add", "README.md")
    _run("git", "-C", str(target_repo), "commit", "-q", "-m", "fixture base")

    batch_result = run_batch(
        BatchTaskSpec(
            target_repo=target_repo,
            prompt="Implement the fixture change",
            max_trajectories=3,
        ),
        BatchDriverConfig(
            cli_argv=(sys.executable, str(FAKE_CLI)),
            run_id="demo-run",
        ),
        ScriptedVerifier(("SUCCESS", "SUCCESS", "CONTINUE")),
    )
    selection = select_candidates(batch_result.run_dir, fixture_judge)

    print(f"run: {batch_result.run_dir}")
    print(f"trajectories: {batch_result.trajectories_run}")
    print(f"candidates: {batch_result.candidates_recorded}")
    print(f"selection: {selection.path}")
    print(
        "metrics: "
        f"pass={selection.metrics['pass']:.4f} "
        f"select={selection.metrics['select']:.4f} "
        f"first={selection.metrics['first']:.4f}"
    )
    print(f"selected candidate: {selection.selected_candidate}")
    return 0


def _run(*command: str) -> None:
    subprocess.run(command, check=True, capture_output=True, text=True)


if __name__ == "__main__":
    raise SystemExit(main())
