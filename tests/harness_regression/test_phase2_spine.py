from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from middleware.telemetry import (
    TelemetryEvent,
    TelemetryValidationError,
    load_terminal_outcome,
    validate_telemetry_file,
)
from middleware.terminal_outcome import (
    OutcomeEvidence,
    OutcomeSignals,
    Reward,
    TerminalOutcome,
    TerminationReason,
    classify_terminal_outcome,
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


def test_provider_telemetry_accepts_measured_throughput() -> None:
    provider_payload = {
        "requestId": "request-1",
        "payloadDigest": "a" * 64,
        "provider": "faux",
        "model": "faux-model",
        "status": 200,
        "ttftMs": 1000.0,
        "generationMs": 1000.0,
        "prefillTokens": 100,
        "decodeTokens": 10,
        "prefillTokensPerSecond": 100.0,
        "decodeTokensPerSecond": 10.0,
    }
    provider = {
        **telemetry_event(1, "provider"),
        "durationMs": 2000.0,
        "provider": provider_payload,
    }

    parsed = TelemetryEvent.from_mapping(provider)

    assert parsed.provider is not None
    assert parsed.provider.ttft_ms == 1000.0
    assert parsed.provider.prefill_tokens == 100
    assert parsed.provider.decode_tokens_per_second == 10.0

    invalid = dict(provider)
    invalid["provider"] = {**provider_payload, "ttftMs": -1}
    with pytest.raises(TelemetryValidationError, match="nonnegative number"):
        TelemetryEvent.from_mapping(invalid)


@pytest.mark.parametrize(
    ("reason", "reward"),
    [
        (TerminationReason.SUCCESS, 1),
        (TerminationReason.FAILED_ASSERTION, 0),
        (TerminationReason.TIMEOUT, 0),
        (TerminationReason.BUDGET_EXHAUSTED, None),
        (TerminationReason.TOOL_ERROR, None),
        (TerminationReason.UNKNOWN, None),
    ],
)
def test_terminal_outcome_contract_accepts_all_reasons(
    reason: TerminationReason, reward: Reward
) -> None:
    outcome = TerminalOutcome(
        reward,
        reason,
        OutcomeEvidence("transcript", "/tmp/session.jsonl", "entry-1"),
    )
    assert TerminalOutcome.from_mapping(outcome.as_dict()) == outcome


@pytest.mark.parametrize(
    ("reason", "reward"),
    [
        (TerminationReason.SUCCESS, 0),
        (TerminationReason.FAILED_ASSERTION, None),
        (TerminationReason.TIMEOUT, None),
        (TerminationReason.BUDGET_EXHAUSTED, 0),
        (TerminationReason.UNKNOWN, 0),
    ],
)
def test_terminal_outcome_contract_rejects_nondeterministic_reward_claims(
    reason: TerminationReason, reward: Reward
) -> None:
    with pytest.raises(ValueError):
        TerminalOutcome(
            reward,
            reason,
            OutcomeEvidence("artifact", "/tmp/result.json"),
        )


def test_terminal_outcome_classifier_and_telemetry_are_single_source(
    tmp_path: Path,
) -> None:
    evidence = OutcomeEvidence("artifact", "/tmp/acceptance.json")
    outcome = classify_terminal_outcome(
        OutcomeSignals(
            agent_exit_code=0,
            agent_timed_out=False,
            acceptance_configured=True,
            acceptance_exit_code=0,
            acceptance_timed_out=False,
        ),
        evidence,
    )
    first = telemetry_event(1, "session.start")
    terminal = {
        **telemetry_event(2, "session.outcome"),
        "outcome": outcome.as_dict(),
    }
    path = tmp_path / "session-1.jsonl"
    path.write_text(f"{json.dumps(first)}\n{json.dumps(terminal)}\n")

    assert validate_telemetry_file(path).events == 2
    assert load_terminal_outcome(tmp_path, "session-1") == outcome

    path.write_text(
        f"{json.dumps(first)}\n{json.dumps(terminal)}\n"
        f"{json.dumps({**terminal, 'eventId': 'event-3', 'sequence': 3})}\n"
    )
    with pytest.raises(TelemetryValidationError, match="multiple terminal outcomes"):
        validate_telemetry_file(path)
