from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from local_memory.store import MemoryStore
from synth.service import BridgeService, ServiceError

ROOT = Path(__file__).resolve().parents[2]
TSX = Path(
    os.environ.get(
        "SYNTH_TEST_TSX",
        str(ROOT / "node_modules" / ".bin" / "tsx"),
    )
)


def _run_ts_driver(
    script_name: str,
    *,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [str(TSX), str(ROOT / "scripts" / script_name)],
        cwd=ROOT,
        env={
            **os.environ,
            "PI_OFFLINE": "1",
            "UV_OFFLINE": "1",
            **(environment or {}),
        },
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return completed


def test_locked_database_returns_retryable_memory_busy(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    database = workspace / ".synth" / "synth.db"
    workspace.mkdir()
    (workspace / "synth.json").write_text(
        json.dumps(
            {
                "memory": {
                    "enabled": True,
                    "k": 3,
                    "minScore": 0.35,
                    "db": ".synth/synth.db",
                    "embedModel": "BAAI/bge-small-en-v1.5",
                    "judgeModel": "small",
                }
            }
        ),
        encoding="utf-8",
    )
    with MemoryStore(database, embedding_dimension=384, workspace=workspace):
        pass
    locker = sqlite3.connect(database, timeout=0)
    service = BridgeService(workspace, memory_busy_timeout_ms=20)
    try:
        locker.execute("PRAGMA locking_mode = EXCLUSIVE")
        locker.execute("BEGIN EXCLUSIVE")
        locker.execute("CREATE TABLE phase8_lock_probe(value INTEGER)")

        with pytest.raises(ServiceError) as caught:
            service.handle("memory.list", {})

        assert caught.value.code == "memory_busy"
        assert caught.value.retryable is True
    finally:
        service.close()
        locker.rollback()
        locker.close()


def test_bridge_crash_mid_session_preserves_the_agent(tmp_path: Path) -> None:
    completed = _run_ts_driver(
        "synth-phase2-demo.ts",
        environment={
            "SYNTH_PHASE2_TELEMETRY_OUT": str(tmp_path / "telemetry.jsonl")
        },
    )

    assert "Phase 2 extension spine PASS" in completed.stdout
    assert completed.stderr.count("bridge exited unexpectedly (SIGKILL)") == 1


def test_endpoint_down_mid_turn_preserves_the_agent_conclusion() -> None:
    completed = _run_ts_driver("synth-phase8-endpoint-down.ts")

    assert "endpoint-down PASS agent=completed verifier=degraded warnings=1" in (
        completed.stdout
    )


def test_budget_exhausted_mid_tool_denies_followups_and_wraps_up(
    tmp_path: Path,
) -> None:
    completed = _run_ts_driver(
        "synth-phase3-demo.ts",
        environment={
            "SYNTH_PHASE3_TELEMETRY_OUT": str(tmp_path / "telemetry.jsonl")
        },
    )

    assert "Phase 3 BATS PASS contexts=7 budget_events=16 denied=2" in (
        completed.stdout
    )
