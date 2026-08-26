#!/usr/bin/env python3
"""Deterministic end-to-end MaTTS demo through the Synth CLI."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]


def _run(*command: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.strip()
            or completed.stdout.strip()
            or f"command failed: {' '.join(command)}"
        )
    return completed


def _write_fixture_repo(repo: Path) -> None:
    repo.mkdir()
    (repo / ".gitignore").write_text(".synth/\n")
    (repo / "synth.json").write_text(
        json.dumps(
            {
                "memory": {
                    "enabled": True,
                    "k": 3,
                    "minScore": 0.0,
                    "db": ".synth/synth.db",
                    "embedModel": "BAAI/bge-small-en-v1.5",
                    "judgeModel": "small",
                },
                "verifier": {
                    "interactive": False,
                    "batch": True,
                    "maxCycles": 2,
                    "contGen": {"enabled": False, "minBudgetFrac": 0.2},
                },
            },
            indent=2,
        )
        + "\n"
    )
    _run("git", "init", "-q", str(repo), cwd=repo.parent)
    _run("git", "add", ".gitignore", "synth.json", cwd=repo)
    _run(
        "git",
        "-c",
        "user.name=Synth Demo",
        "-c",
        "user.email=synth-demo@example.invalid",
        "commit",
        "-q",
        "-m",
        "fixture base",
        cwd=repo,
    )


def _write_model_fixture(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "judge": [
                    'Thoughts: first trajectory is grounded\nStatus: "success"',
                    'Thoughts: second trajectory lacks evidence\nStatus: "failure"',
                    'Thoughts: third trajectory needs review\nStatus: "failure"',
                ],
                "reverify": [
                    'Thoughts: first verdict is confirmed\nStatus: "success"',
                    'Thoughts: second verdict is confirmed\nStatus: "failure"',
                    'Thoughts: third trajectory is corrected\nStatus: "success"',
                ],
                "parallel": (
                    "# Memory Item 1\n"
                    "## Title Inspect interface contracts before editing\n"
                    "## Description Use when a defect crosses a component boundary.\n"
                    "## Content Trace the concrete contract and its caller before "
                    "changing behavior.\n\n"
                    "# Memory Item 2\n"
                    "## Title Verify the narrow acceptance criterion\n"
                    "## Description Use after a targeted implementation change.\n"
                    "## Content Run the focused check and preserve its exact result "
                    "before concluding.\n"
                ),
                "distill": (
                    "# Memory Item 1\n"
                    "## Title Unused fallback\n"
                    "## Description This fixture branch is not selected.\n"
                    "## Content MaTTS selects the parallel response instead.\n"
                ),
            },
            indent=2,
        )
        + "\n"
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="synth-matts-demo-") as temporary:
        temporary_root = Path(temporary)
        repo = temporary_root / "target"
        fixture = temporary_root / "model-fixture.json"
        _write_fixture_repo(repo)
        _write_model_fixture(fixture)
        environment = os.environ.copy()
        environment["SYNTH_FAKE_DELAY_SECONDS"] = "0.15"
        environment["SYNTH_FASTEMBED_CACHE_DIR"] = str(
            ROOT / ".synth" / "models" / "fastembed"
        )
        completed = subprocess.run(
            [
                str(ROOT / ".venv" / "bin" / "synth"),
                "matts",
                "--task",
                "Repair the seeded behavior and verify the focused acceptance.",
                "-k",
                "3",
                "--target",
                str(repo),
                "--run-id",
                "matts-demo",
                "--fixture-cli",
                str(ROOT / "tests" / "batch_fixtures" / "fake_coding_agent.py"),
                "--model-fixture",
                str(fixture),
                "--timeout-seconds",
                "30",
            ],
            cwd=repo,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
        decoded: object = json.loads(completed.stdout)
        if not isinstance(decoded, dict):
            raise RuntimeError("MaTTS CLI output is not a JSON object")
        report = cast(dict[str, object], decoded)
        if report.get("parallelTrajectories") != 3:
            raise RuntimeError("MaTTS did not preserve all parallel trajectories")
        if report.get("maxConcurrentTrajectories") != 3:
            raise RuntimeError("MaTTS trajectories did not overlap")
        trajectories = report.get("trajectories")
        if not isinstance(trajectories, list) or len(trajectories) != 3:
            raise RuntimeError("MaTTS report has incomplete trajectory receipts")
        final_verdicts = []
        for raw_trajectory in trajectories:
            if not isinstance(raw_trajectory, dict):
                raise RuntimeError("MaTTS trajectory receipt is invalid")
            reverified = raw_trajectory.get("reverifiedJudgment")
            if not isinstance(reverified, dict):
                raise RuntimeError("MaTTS re-verification receipt is missing")
            final_verdicts.append(reverified.get("verdict"))
        if final_verdicts != ["success", "failure", "success"]:
            raise RuntimeError("MaTTS sequential re-verification order drifted")
        memory = report.get("memory")
        if (
            not isinstance(memory, dict)
            or len(cast(list[object], memory["itemIds"])) != 2
        ):
            raise RuntimeError("MaTTS contrastive induction did not yield two items")
        report_path = Path(cast(str, report["reportPath"]))
        if not report_path.is_file():
            raise RuntimeError("MaTTS durable report is missing")
        print(
            "synth-demo: MaTTS PASS parallel=3 max_concurrent=3 reverified=3 memories=2"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
