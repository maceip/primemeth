from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from local_memory.embedder import EmbeddingBatch
from local_memory.store import MemoryStore
from synth.matts import run_matts

_ROOT = Path(__file__).resolve().parents[2]
_FAKE_CLI = _ROOT / "tests" / "batch_fixtures" / "fake_coding_agent.py"


class MattsStubEmbedder:
    def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * 384
            vector[0 if "boundary" in text.lower() else 1] = 1.0
            vectors.append(vector)
        return EmbeddingBatch(
            model="matts-stub",
            dimension=384,
            embeddings=vectors,
        )


def test_matts_runs_parallel_reverifies_and_contrast_distills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "target"
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
            }
        )
        + "\n"
    )
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "add", ".gitignore", "synth.json"],
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
    fixture = tmp_path / "memory-fixture.json"
    fixture.write_text(
        json.dumps(
            {
                "judge": [
                    'Thoughts: initial sample one passed\nStatus: "success"',
                    'Thoughts: initial sample two failed\nStatus: "failure"',
                    'Thoughts: initial sample three failed\nStatus: "failure"',
                ],
                "reverify": [
                    'Thoughts: sample one remains correct\nStatus: "success"',
                    'Thoughts: sample two remains incorrect\nStatus: "failure"',
                    'Thoughts: sample three is corrected\nStatus: "success"',
                ],
                "parallel": (
                    "# Memory Item 1\n"
                    "## Title Inspect the boundary first\n"
                    "## Description Use when behavior depends on an interface.\n"
                    "## Content Inspect the boundary before choosing an edit.\n\n"
                    "# Memory Item 2\n"
                    "## Title Verify the corrected behavior\n"
                    "## Description Use after the smallest targeted change.\n"
                    "## Content Run the focused acceptance check and report it.\n"
                ),
                "distill": (
                    "# Memory Item 1\n"
                    "## Title Unused fallback\n"
                    "## Description This response is not selected.\n"
                    "## Content The MaTTS fixture uses its parallel response.\n"
                ),
            }
        )
        + "\n"
    )
    monkeypatch.setenv("SYNTH_FAKE_DELAY_SECONDS", "0.15")

    result = run_matts(
        workspace=repo,
        task="Repair the seeded behavior and verify it.",
        k=3,
        base_ref="HEAD",
        run_id="matts-fixture",
        timeout_seconds=30.0,
        fixture_cli=_FAKE_CLI,
        model_fixture=fixture,
        embedder=MattsStubEmbedder(),
    )

    assert result["parallelTrajectories"] == 3
    assert result["maxConcurrentTrajectories"] == 3
    assert result["sequentialReverificationRoundsPerTrajectory"] == 1
    trajectories = result["trajectories"]
    assert isinstance(trajectories, list)
    assert [item["reverifiedJudgment"]["verdict"] for item in trajectories] == [
        "success",
        "failure",
        "success",
    ]
    memory = result["memory"]
    assert isinstance(memory, dict)
    assert memory["inserted"] == 2
    assert memory["status"] == "success"
    report_path = Path(str(result["reportPath"]))
    assert report_path.is_file()
    prompt = report_path.with_name("contrast-prompt.md").read_text()
    assert prompt.count("**Trajectory ") == 3
    assert "**Initial Judgment:** failure" in prompt
    assert "**Sequential Re-verification:** success" in prompt
    with MemoryStore(
        repo / ".synth" / "synth.db",
        embedder=MattsStubEmbedder(),
    ) as store:
        items = store.list()
    assert len(items) == 2
    assert {item.status for item in items} == {"success"}
    assert {item.session_id for item in items} == {"matts:matts-fixture"}
