from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

from synth.effectiveness import (
    EndpointSpec,
    TrialOutcome,
    _capture_source_snapshot,
    _evaluation_complete,
    _materialize_trial_repo,
    load_task_fixtures,
    session_metrics,
    snapshot_memory_database,
)

_ROOT = Path(__file__).resolve().parents[2]


def test_materialized_task_excludes_manifest_and_embedded_solution(
    tmp_path: Path,
) -> None:
    fixture = load_task_fixtures(
        _ROOT / "tests" / "harness_tasks" / "train", ("train-add",)
    )[0]
    repo = tmp_path / "repo"
    _materialize_trial_repo(
        root=_ROOT,
        repo=repo,
        fixture=fixture,
        memory_enabled=False,
        endpoint=EndpointSpec(
            base_url="http://localhost:8000/v1",
            api="openai-completions",
            model="local-small",
            api_key_env="SYNTH_LOCAL_API_KEY",
        ),
    )

    tracked = subprocess.run(
        ["git", "-C", str(repo), "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    rendered = "\n".join(
        path.read_text(errors="replace")
        for path in repo.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(repo).parts
    )
    assert not list(repo.rglob("task.yaml"))
    assert "task.yaml" not in tracked
    assert "return left + right" not in rendered
    assert "return left - right" in (repo / "math_ops.py").read_text()


def test_memory_snapshot_includes_committed_wal_state(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "snapshot.db"
    connection = sqlite3.connect(source)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute("CREATE TABLE receipts (value TEXT NOT NULL)")
        connection.execute("INSERT INTO receipts VALUES ('durable')")
        connection.commit()
        digest = snapshot_memory_database(source, destination)
    finally:
        connection.close()

    with sqlite3.connect(destination) as snapshot:
        assert snapshot.execute("SELECT value FROM receipts").fetchone() == ("durable",)
    assert len(digest) == 64


def test_session_metrics_use_active_native_usage_and_memory_receipt(
    tmp_path: Path,
) -> None:
    session = tmp_path / "session.jsonl"
    records = [
        {
            "type": "session",
            "version": 3,
            "id": "fixture",
            "cwd": str(tmp_path),
            "parentSession": None,
        },
        {
            "type": "custom",
            "id": "memory",
            "parentId": None,
            "customType": "synth.memory.retrieval",
            "data": {"itemIds": [3, 7], "section": "## Lessons"},
        },
        {
            "type": "message",
            "id": "assistant",
            "parentId": "memory",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "toolCall", "name": "ipython", "arguments": {}},
                    {"type": "text", "text": "done"},
                ],
                "usage": {
                    "input": 10,
                    "output": 5,
                    "cacheRead": 2,
                    "cacheWrite": 0,
                    "totalTokens": 17,
                    "cost": {
                        "input": 0.1,
                        "output": 0.2,
                        "cacheRead": 0.01,
                        "cacheWrite": 0,
                        "total": 0.31,
                    },
                },
            },
        },
    ]
    session.write_text("".join(json.dumps(record) + "\n" for record in records))

    metrics = session_metrics(session)
    assert metrics.tool_calls == 1
    assert metrics.total_tokens == 17
    assert metrics.cost_usd == 0.31
    assert metrics.memory_item_ids == (3, 7)


def test_evaluation_complete_rejects_acceptance_timeout_and_integrity_failure(
    tmp_path: Path,
) -> None:
    session = tmp_path / "session.jsonl"
    session.write_text("fixture\n")
    completed = TrialOutcome(
        ordinal=1,
        repetition=1,
        task_id="fixture",
        condition="memory_off",
        success=True,
        agent_exit_code=0,
        acceptance_exit_code=0,
        acceptance_timed_out=False,
        acceptance_integrity_failures=(),
        stop_reason="continuation_disabled",
        tool_calls=0,
        tool_names=(),
        total_tokens=0,
        cost_usd=0.0,
        memory_item_ids=(),
        apis=(),
        providers=(),
        models=(),
        session_jsonl=session,
        artifact_dir=tmp_path,
        diff_sha256="0" * 64,
    )

    assert _evaluation_complete([completed]) is True
    assert (
        _evaluation_complete(
            [replace(completed, success=False, acceptance_exit_code=1)]
        )
        is True
    )
    assert (
        _evaluation_complete(
            [
                replace(
                    completed,
                    success=False,
                    acceptance_exit_code=124,
                    acceptance_timed_out=True,
                    stop_reason="acceptance_timeout",
                )
            ]
        )
        is False
    )
    assert (
        _evaluation_complete(
            [
                replace(
                    completed,
                    success=False,
                    acceptance_exit_code=125,
                    acceptance_integrity_failures=("test_fixture.py",),
                    stop_reason="acceptance_integrity_failure",
                )
            ]
        )
        is False
    )


def test_source_snapshot_hashes_dirty_and_untracked_content(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    source = repo / "source.py"
    cli = repo / "cli.js"
    source.write_text("value = 1\n")
    cli.write_text("export {};\n")
    subprocess.run(["git", "-C", str(repo), "add", "source.py", "cli.js"], check=True)
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
    clean = _capture_source_snapshot(repo, cli)
    source.write_text("value = 2\n")
    untracked = repo / "new.py"
    untracked.write_text("new = True\n")
    dirty = _capture_source_snapshot(repo, cli)

    assert dirty.workspace_content_sha256 != clean.workspace_content_sha256
    assert dirty.tracked_diff_sha256 != clean.tracked_diff_sha256
    assert dict(dirty.untracked_files) == {
        "new.py": dict(dirty.untracked_files)["new.py"]
    }
    assert len(dict(dirty.untracked_files)["new.py"]) == 64
