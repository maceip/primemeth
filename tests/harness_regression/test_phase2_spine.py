from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from middleware.telemetry import (
    TelemetryEvent,
    TelemetryValidationError,
    validate_telemetry_file,
)
from synth.bridge import run_bridge


class BinaryInput:
    def __init__(self, content: bytes) -> None:
        self.buffer = io.BytesIO(content)


def test_bridge_json_lines_recovers_after_a_malformed_frame(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    frames = (
        b"not-json\n"
        + json.dumps(
            {
                "id": "ping-1",
                "command": "ping",
                "params": {"protocolVersion": 1},
            }
        ).encode()
        + b"\n"
    )
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdin", BinaryInput(frames))
    monkeypatch.setattr(sys, "stdout", output)

    assert run_bridge(tmp_path) == 0

    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses[0] == {
        "id": None,
        "ok": False,
        "error": {
            "code": "invalid_json",
            "message": "request is not valid JSON",
            "retryable": False,
        },
    }
    assert responses[1]["id"] == "ping-1"
    assert responses[1]["ok"] is True
    assert responses[1]["result"]["protocolVersion"] == 1
    assert responses[1]["result"]["memoryAvailable"] is False


def telemetry_event(sequence: int, kind: str) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "eventId": f"event-{sequence}",
        "sequence": sequence,
        "timestamp": "2026-08-26T00:00:00.000Z",
        "sessionId": "session-1",
        "kind": kind,
    }


def test_mirrored_telemetry_schema_enforces_shape_and_sequence(
    tmp_path: Path,
) -> None:
    first = telemetry_event(1, "session.start")
    second = telemetry_event(2, "session.shutdown")
    path = tmp_path / "telemetry.jsonl"
    path.write_text(f"{json.dumps(first)}\n{json.dumps(second)}\n")

    parsed = TelemetryEvent.from_mapping(first)
    assert parsed.kind == "session.start"
    assert validate_telemetry_file(path).events == 2

    invalid = dict(first, unexpected=True)
    with pytest.raises(TelemetryValidationError, match="unexpected fields"):
        TelemetryEvent.from_mapping(invalid)

    nullable = dict(first, durationMs=None)
    with pytest.raises(TelemetryValidationError, match="nonnegative number"):
        TelemetryEvent.from_mapping(nullable)

    nonfinite = dict(first, durationMs=float("nan"))
    with pytest.raises(TelemetryValidationError, match="nonnegative number"):
        TelemetryEvent.from_mapping(nonfinite)

    path.write_text(f"{json.dumps(first)}\n{json.dumps(telemetry_event(3, 'turn'))}\n")
    with pytest.raises(TelemetryValidationError, match="expected sequence 2"):
        validate_telemetry_file(path)
