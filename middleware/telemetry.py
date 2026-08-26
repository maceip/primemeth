"""Strict Python mirror of the extension-owned Synth telemetry schema."""

from __future__ import annotations

import json
import math
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from middleware.terminal_outcome import TerminalOutcome

TELEMETRY_SCHEMA_VERSION = 1
TELEMETRY_KINDS = {
    "session.start",
    "session.shutdown",
    "session.outcome",
    "turn",
    "tool",
    "provider",
    "agent.end",
    "bridge",
    "budget",
}


class TelemetryValidationError(ValueError):
    """Raised when a telemetry line does not match the mirrored schema."""


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise TelemetryValidationError(f"{label} must be an object")
    return value


def _keys(
    value: dict[str, object],
    *,
    label: str,
    required: set[str],
    optional: Collection[str] = (),
) -> None:
    missing = required - set(value)
    unexpected = set(value) - required - set(optional)
    if missing:
        raise TelemetryValidationError(
            f"{label} is missing: {', '.join(sorted(missing))}"
        )
    if unexpected:
        raise TelemetryValidationError(
            f"{label} has unexpected fields: {', '.join(sorted(unexpected))}"
        )


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise TelemetryValidationError(f"{label} must be a nonempty string")
    return value


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TelemetryValidationError(f"{label} must be a nonnegative integer")
    return value


def _number(value: object, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or value < 0
    ):
        raise TelemetryValidationError(f"{label} must be a nonnegative number")
    return float(value)


@dataclass(frozen=True, slots=True)
class UsageCost:
    input: float
    output: float
    cache_read: float
    cache_write: float
    total: float

    @classmethod
    def from_mapping(cls, value: object) -> UsageCost:
        mapping = _mapping(value, "usage.cost")
        fields = {"input", "output", "cacheRead", "cacheWrite", "total"}
        _keys(mapping, label="usage.cost", required=fields)
        return cls(
            input=_number(mapping["input"], "usage.cost.input"),
            output=_number(mapping["output"], "usage.cost.output"),
            cache_read=_number(mapping["cacheRead"], "usage.cost.cacheRead"),
            cache_write=_number(mapping["cacheWrite"], "usage.cost.cacheWrite"),
            total=_number(mapping["total"], "usage.cost.total"),
        )


@dataclass(frozen=True, slots=True)
class Usage:
    input: int
    output: int
    cache_read: int
    cache_write: int
    total_tokens: int
    cost: UsageCost

    @classmethod
    def from_mapping(cls, value: object) -> Usage:
        mapping = _mapping(value, "usage")
        fields = {"input", "output", "cacheRead", "cacheWrite", "totalTokens", "cost"}
        _keys(mapping, label="usage", required=fields)
        return cls(
            input=_integer(mapping["input"], "usage.input"),
            output=_integer(mapping["output"], "usage.output"),
            cache_read=_integer(mapping["cacheRead"], "usage.cacheRead"),
            cache_write=_integer(mapping["cacheWrite"], "usage.cacheWrite"),
            total_tokens=_integer(mapping["totalTokens"], "usage.totalTokens"),
            cost=UsageCost.from_mapping(mapping["cost"]),
        )


@dataclass(frozen=True, slots=True)
class TranscriptPointer:
    session_file: str
    entry_id: str | None

    @classmethod
    def from_mapping(cls, value: object) -> TranscriptPointer:
        mapping = _mapping(value, "transcript")
        _keys(mapping, label="transcript", required={"sessionFile", "entryId"})
        entry_id = mapping["entryId"]
        if entry_id is not None and not isinstance(entry_id, str):
            raise TelemetryValidationError(
                "transcript.entryId must be a string or null"
            )
        return cls(
            session_file=_string(mapping["sessionFile"], "transcript.sessionFile"),
            entry_id=entry_id,
        )


@dataclass(frozen=True, slots=True)
class ToolExchange:
    tool_call_id: str
    name: str
    args_digest: str
    is_error: bool

    @classmethod
    def from_mapping(cls, value: object) -> ToolExchange:
        mapping = _mapping(value, "tool")
        fields = {"toolCallId", "name", "argsDigest", "isError"}
        _keys(mapping, label="tool", required=fields)
        is_error = mapping["isError"]
        if not isinstance(is_error, bool):
            raise TelemetryValidationError("tool.isError must be a boolean")
        return cls(
            tool_call_id=_string(mapping["toolCallId"], "tool.toolCallId"),
            name=_string(mapping["name"], "tool.name"),
            args_digest=_string(mapping["argsDigest"], "tool.argsDigest"),
            is_error=is_error,
        )


@dataclass(frozen=True, slots=True)
class ProviderExchange:
    request_id: str
    payload_digest: str
    provider: str | None
    model: str | None
    status: int
    ttft_ms: float | None
    generation_ms: float | None
    prefill_tokens: int | None
    decode_tokens: int | None
    prefill_tokens_per_second: float | None
    decode_tokens_per_second: float | None

    @classmethod
    def from_mapping(cls, value: object) -> ProviderExchange:
        mapping = _mapping(value, "provider")
        fields = {"requestId", "payloadDigest", "provider", "model", "status"}
        optional = {
            "ttftMs",
            "generationMs",
            "prefillTokens",
            "decodeTokens",
            "prefillTokensPerSecond",
            "decodeTokensPerSecond",
        }
        _keys(mapping, label="provider", required=fields, optional=optional)
        provider = mapping["provider"]
        model = mapping["model"]
        if provider is not None and not isinstance(provider, str):
            raise TelemetryValidationError("provider.provider must be a string or null")
        if model is not None and not isinstance(model, str):
            raise TelemetryValidationError("provider.model must be a string or null")
        return cls(
            request_id=_string(mapping["requestId"], "provider.requestId"),
            payload_digest=_string(mapping["payloadDigest"], "provider.payloadDigest"),
            provider=provider,
            model=model,
            status=_integer(mapping["status"], "provider.status"),
            ttft_ms=(
                _number(mapping["ttftMs"], "provider.ttftMs")
                if "ttftMs" in mapping
                else None
            ),
            generation_ms=(
                _number(mapping["generationMs"], "provider.generationMs")
                if "generationMs" in mapping
                else None
            ),
            prefill_tokens=(
                _integer(mapping["prefillTokens"], "provider.prefillTokens")
                if "prefillTokens" in mapping
                else None
            ),
            decode_tokens=(
                _integer(mapping["decodeTokens"], "provider.decodeTokens")
                if "decodeTokens" in mapping
                else None
            ),
            prefill_tokens_per_second=(
                _number(
                    mapping["prefillTokensPerSecond"],
                    "provider.prefillTokensPerSecond",
                )
                if "prefillTokensPerSecond" in mapping
                else None
            ),
            decode_tokens_per_second=(
                _number(
                    mapping["decodeTokensPerSecond"],
                    "provider.decodeTokensPerSecond",
                )
                if "decodeTokensPerSecond" in mapping
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class BudgetCounter:
    limit: float
    used: float
    remaining: float | None
    unlimited: bool

    @classmethod
    def from_mapping(cls, value: object, label: str) -> BudgetCounter:
        mapping = _mapping(value, label)
        _keys(
            mapping,
            label=label,
            required={"limit", "used", "remaining", "unlimited"},
        )
        remaining_value = mapping["remaining"]
        remaining = (
            None
            if remaining_value is None
            else _number(remaining_value, f"{label}.remaining")
        )
        unlimited = mapping["unlimited"]
        if not isinstance(unlimited, bool):
            raise TelemetryValidationError(f"{label}.unlimited must be a boolean")
        limit = _number(mapping["limit"], f"{label}.limit")
        if unlimited != (limit == 0):
            raise TelemetryValidationError(
                f"{label}.unlimited must match whether limit is zero"
            )
        if unlimited != (remaining is None):
            raise TelemetryValidationError(
                f"{label}.remaining must be null exactly when unlimited"
            )
        return cls(
            limit=limit,
            used=_number(mapping["used"], f"{label}.used"),
            remaining=remaining,
            unlimited=unlimited,
        )


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    bash: BudgetCounter
    edit: BudgetCounter
    ipython: BudgetCounter
    subagents: BudgetCounter
    tokens: BudgetCounter
    cost_usd: BudgetCounter
    regime: str
    fully_exhausted: bool

    @classmethod
    def from_mapping(cls, value: object) -> BudgetSnapshot:
        mapping = _mapping(value, "budget")
        counter_names = {
            "bash",
            "edit",
            "ipython",
            "subagents",
            "tokens",
            "costUsd",
        }
        _keys(
            mapping,
            label="budget",
            required=counter_names | {"regime", "fullyExhausted"},
        )
        regime = _string(mapping["regime"], "budget.regime")
        if regime not in {"HIGH", "MEDIUM", "LOW", "CRITICAL"}:
            raise TelemetryValidationError(f"unknown budget regime: {regime}")
        fully_exhausted = mapping["fullyExhausted"]
        if not isinstance(fully_exhausted, bool):
            raise TelemetryValidationError("budget.fullyExhausted must be a boolean")
        return cls(
            bash=BudgetCounter.from_mapping(mapping["bash"], "budget.bash"),
            edit=BudgetCounter.from_mapping(mapping["edit"], "budget.edit"),
            ipython=BudgetCounter.from_mapping(mapping["ipython"], "budget.ipython"),
            subagents=BudgetCounter.from_mapping(
                mapping["subagents"], "budget.subagents"
            ),
            tokens=BudgetCounter.from_mapping(mapping["tokens"], "budget.tokens"),
            cost_usd=BudgetCounter.from_mapping(mapping["costUsd"], "budget.costUsd"),
            regime=regime,
            fully_exhausted=fully_exhausted,
        )


@dataclass(frozen=True, slots=True)
class TelemetryEvent:
    schema_version: int
    event_id: str
    sequence: int
    timestamp: str
    session_id: str
    kind: str
    turn_index: int | None
    duration_ms: float | None
    usage: Usage | None
    tool: ToolExchange | None
    provider: ProviderExchange | None
    budget: BudgetSnapshot | None
    outcome: TerminalOutcome | None
    transcript: TranscriptPointer | None
    error_class: str | None
    data: dict[str, object] | None

    @classmethod
    def from_mapping(cls, value: object) -> TelemetryEvent:
        mapping = _mapping(value, "event")
        required = {
            "schemaVersion",
            "eventId",
            "sequence",
            "timestamp",
            "sessionId",
            "kind",
        }
        optional = {
            "turnIndex",
            "durationMs",
            "usage",
            "tool",
            "provider",
            "budget",
            "outcome",
            "transcript",
            "errorClass",
            "data",
        }
        _keys(mapping, label="event", required=required, optional=optional)
        schema_version = _integer(mapping["schemaVersion"], "event.schemaVersion")
        if schema_version != TELEMETRY_SCHEMA_VERSION:
            raise TelemetryValidationError(
                f"unsupported telemetry schema version: {schema_version}"
            )
        kind = _string(mapping["kind"], "event.kind")
        if kind not in TELEMETRY_KINDS:
            raise TelemetryValidationError(f"unknown telemetry kind: {kind}")
        turn_index = mapping.get("turnIndex")
        duration_ms = mapping.get("durationMs")
        error_class = mapping.get("errorClass")
        if error_class is not None and not isinstance(error_class, str):
            raise TelemetryValidationError("event.errorClass must be a string")
        parsed_data = (
            _mapping(mapping["data"], "event.data") if "data" in mapping else None
        )
        event = cls(
            schema_version=schema_version,
            event_id=_string(mapping["eventId"], "event.eventId"),
            sequence=_integer(mapping["sequence"], "event.sequence"),
            timestamp=_string(mapping["timestamp"], "event.timestamp"),
            session_id=_string(mapping["sessionId"], "event.sessionId"),
            kind=kind,
            turn_index=(
                _integer(turn_index, "event.turnIndex")
                if "turnIndex" in mapping
                else None
            ),
            duration_ms=(
                _number(duration_ms, "event.durationMs")
                if "durationMs" in mapping
                else None
            ),
            usage=(
                Usage.from_mapping(mapping["usage"]) if "usage" in mapping else None
            ),
            tool=(
                ToolExchange.from_mapping(mapping["tool"])
                if "tool" in mapping
                else None
            ),
            provider=(
                ProviderExchange.from_mapping(mapping["provider"])
                if "provider" in mapping
                else None
            ),
            budget=(
                BudgetSnapshot.from_mapping(mapping["budget"])
                if "budget" in mapping
                else None
            ),
            outcome=(
                _terminal_outcome(mapping["outcome"])
                if "outcome" in mapping
                else None
            ),
            transcript=(
                TranscriptPointer.from_mapping(mapping["transcript"])
                if "transcript" in mapping
                else None
            ),
            error_class=error_class,
            data=parsed_data,
        )
        if event.kind == "tool" and event.tool is None:
            raise TelemetryValidationError("tool event requires tool details")
        if event.kind == "provider" and event.provider is None:
            raise TelemetryValidationError("provider event requires provider details")
        if event.kind == "budget" and event.budget is None:
            raise TelemetryValidationError("budget event requires a budget snapshot")
        if event.kind == "session.outcome" and event.outcome is None:
            raise TelemetryValidationError(
                "session.outcome event requires a terminal outcome"
            )
        if event.kind != "session.outcome" and event.outcome is not None:
            raise TelemetryValidationError(
                "only session.outcome events may contain a terminal outcome"
            )
        return event

    @classmethod
    def from_json_line(cls, line: str) -> TelemetryEvent:
        try:
            value: object = json.loads(line)
        except json.JSONDecodeError as error:
            raise TelemetryValidationError(
                "telemetry line is not valid JSON"
            ) from error
        return cls.from_mapping(value)


@dataclass(frozen=True, slots=True)
class TelemetryValidation:
    events: int
    sessions: tuple[str, ...]


def validate_telemetry_file(path: Path) -> TelemetryValidation:
    counts: dict[str, int] = {}
    terminal_sessions: set[str] = set()
    events = 0
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                event = TelemetryEvent.from_json_line(line)
            except TelemetryValidationError as error:
                raise TelemetryValidationError(
                    f"{path}:{line_number}: {error}"
                ) from error
            expected_sequence = counts.get(event.session_id, 0) + 1
            if event.sequence != expected_sequence:
                raise TelemetryValidationError(
                    f"{path}:{line_number}: expected sequence {expected_sequence}, "
                    f"got {event.sequence}"
                )
            counts[event.session_id] = event.sequence
            if event.kind == "session.outcome":
                if event.session_id in terminal_sessions:
                    raise TelemetryValidationError(
                        f"{path}:{line_number}: session has multiple terminal outcomes"
                    )
                terminal_sessions.add(event.session_id)
            events += 1
    if events == 0:
        raise TelemetryValidationError(f"{path}: contains no telemetry events")
    return TelemetryValidation(events=events, sessions=tuple(sorted(counts)))


def load_terminal_outcome(directory: Path, session_id: str) -> TerminalOutcome | None:
    """Return the sole validated terminal outcome for a session, if present."""
    safe_session_id = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in session_id
    )
    path = directory / f"{safe_session_id}.jsonl"
    if not path.exists():
        return None
    validate_telemetry_file(path)
    found: TerminalOutcome | None = None
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                event = TelemetryEvent.from_json_line(line)
            except TelemetryValidationError as error:
                raise TelemetryValidationError(
                    f"{path}:{line_number}: {error}"
                ) from error
            if event.session_id != session_id or event.kind != "session.outcome":
                continue
            if found is not None:
                raise TelemetryValidationError(
                    f"{path}:{line_number}: session has multiple terminal outcomes"
                )
            found = event.outcome
    return found


def _terminal_outcome(value: object) -> TerminalOutcome:
    try:
        return TerminalOutcome.from_mapping(value)
    except ValueError as error:
        raise TelemetryValidationError(str(error)) from error
