"""Shared deterministic terminal-outcome contract for Synth sessions."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, cast

Reward = Literal[0, 1] | None
EvidenceKind = Literal["transcript", "artifact"]

_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class TerminationReason(StrEnum):
    SUCCESS = "SUCCESS"
    FAILED_ASSERTION = "FAILED_ASSERTION"
    TIMEOUT = "TIMEOUT"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    TOOL_ERROR = "TOOL_ERROR"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class OutcomeEvidence:
    kind: EvidenceKind
    uri: str
    entry_id: str | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("transcript", "artifact"):
            raise ValueError(f"unknown terminal evidence kind: {self.kind}")
        if not self.uri.strip():
            raise ValueError("terminal evidence URI must be nonempty")
        if self.entry_id is not None and not self.entry_id:
            raise ValueError("terminal evidence entry_id must be nonempty or null")
        if self.sha256 is not None and _SHA256_PATTERN.fullmatch(self.sha256) is None:
            raise ValueError("terminal evidence sha256 must be lowercase SHA-256")

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "uri": self.uri,
            "entry_id": self.entry_id,
            **({} if self.sha256 is None else {"sha256": self.sha256}),
        }

    @classmethod
    def from_mapping(cls, value: object) -> OutcomeEvidence:
        mapping = _mapping(value, "terminal outcome evidence")
        required = {"kind", "uri", "entry_id"}
        optional = {"sha256"}
        _exact_keys(mapping, required, optional, "terminal outcome evidence")
        kind = mapping["kind"]
        uri = mapping["uri"]
        entry_id = mapping["entry_id"]
        sha256 = mapping.get("sha256")
        if kind not in ("transcript", "artifact"):
            raise ValueError(f"unknown terminal evidence kind: {kind}")
        if not isinstance(uri, str):
            raise ValueError("terminal evidence uri must be a string")
        if entry_id is not None and not isinstance(entry_id, str):
            raise ValueError("terminal evidence entry_id must be a string or null")
        if sha256 is not None and not isinstance(sha256, str):
            raise ValueError("terminal evidence sha256 must be a string")
        return cls(
            kind=kind,
            uri=uri,
            entry_id=entry_id,
            sha256=sha256,
        )


@dataclass(frozen=True, slots=True)
class TerminalOutcome:
    reward: Reward
    termination_reason: TerminationReason
    evidence: OutcomeEvidence

    def __post_init__(self) -> None:
        if isinstance(self.reward, bool) or self.reward not in (0, 1, None):
            raise ValueError("terminal reward must be 0, 1, or null")
        if self.termination_reason is TerminationReason.SUCCESS and self.reward != 1:
            raise ValueError("SUCCESS requires reward 1")
        if (
            self.reward == 1
            and self.termination_reason is not TerminationReason.SUCCESS
        ):
            raise ValueError("reward 1 requires SUCCESS")
        if self.termination_reason in {
            TerminationReason.FAILED_ASSERTION,
            TerminationReason.TIMEOUT,
        } and self.reward != 0:
            raise ValueError(f"{self.termination_reason} requires reward 0")
        if (
            self.termination_reason is TerminationReason.BUDGET_EXHAUSTED
            and self.reward is not None
        ):
            raise ValueError("BUDGET_EXHAUSTED requires a null reward")
        if (
            self.termination_reason is TerminationReason.UNKNOWN
            and self.reward is not None
        ):
            raise ValueError("UNKNOWN requires a null reward")

    @property
    def succeeded(self) -> bool:
        return self.reward == 1

    def as_dict(self) -> dict[str, object]:
        return {
            "reward": self.reward,
            "termination_reason": self.termination_reason.value,
            "evidence": self.evidence.as_dict(),
        }

    @classmethod
    def from_mapping(cls, value: object) -> TerminalOutcome:
        mapping = _mapping(value, "terminal outcome")
        _exact_keys(
            mapping,
            {"reward", "termination_reason", "evidence"},
            set(),
            "terminal outcome",
        )
        reward = mapping["reward"]
        if isinstance(reward, bool) or reward not in (0, 1, None):
            raise ValueError("terminal reward must be 0, 1, or null")
        reason = mapping["termination_reason"]
        if not isinstance(reason, str):
            raise ValueError("terminal termination_reason must be a string")
        try:
            termination_reason = TerminationReason(reason)
        except ValueError as error:
            raise ValueError(
                f"unknown terminal termination_reason: {reason}"
            ) from error
        return cls(
            reward=reward,
            termination_reason=termination_reason,
            evidence=OutcomeEvidence.from_mapping(mapping["evidence"]),
        )


@dataclass(frozen=True, slots=True)
class OutcomeSignals:
    agent_exit_code: int
    agent_timed_out: bool
    acceptance_configured: bool
    acceptance_exit_code: int | None
    acceptance_timed_out: bool
    acceptance_integrity_failures: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.agent_exit_code, bool):
            raise ValueError("agent_exit_code must be an integer")
        if self.acceptance_exit_code is not None and isinstance(
            self.acceptance_exit_code, bool
        ):
            raise ValueError("acceptance_exit_code must be an integer or null")
        if any(
            not isinstance(value, str) or not value
            for value in self.acceptance_integrity_failures
        ):
            raise ValueError("acceptance integrity failures must be nonempty strings")

    def as_dict(self) -> dict[str, object]:
        return {
            "agent_exit_code": self.agent_exit_code,
            "agent_timed_out": self.agent_timed_out,
            "acceptance_configured": self.acceptance_configured,
            "acceptance_exit_code": self.acceptance_exit_code,
            "acceptance_timed_out": self.acceptance_timed_out,
            "acceptance_integrity_failures": list(
                self.acceptance_integrity_failures
            ),
        }

    @classmethod
    def from_mapping(cls, value: object) -> OutcomeSignals:
        mapping = _mapping(value, "terminal outcome signals")
        fields = {
            "agent_exit_code",
            "agent_timed_out",
            "acceptance_configured",
            "acceptance_exit_code",
            "acceptance_timed_out",
            "acceptance_integrity_failures",
        }
        _exact_keys(mapping, fields, set(), "terminal outcome signals")
        agent_exit_code = _integer(mapping["agent_exit_code"], "agent_exit_code")
        acceptance_exit_code_value = mapping["acceptance_exit_code"]
        acceptance_exit_code = (
            None
            if acceptance_exit_code_value is None
            else _integer(acceptance_exit_code_value, "acceptance_exit_code")
        )
        failures_value = mapping["acceptance_integrity_failures"]
        if not isinstance(failures_value, list) or not all(
            isinstance(item, str) and item for item in failures_value
        ):
            raise ValueError(
                "acceptance_integrity_failures must be an array of nonempty strings"
            )
        return cls(
            agent_exit_code=agent_exit_code,
            agent_timed_out=_boolean(mapping["agent_timed_out"], "agent_timed_out"),
            acceptance_configured=_boolean(
                mapping["acceptance_configured"], "acceptance_configured"
            ),
            acceptance_exit_code=acceptance_exit_code,
            acceptance_timed_out=_boolean(
                mapping["acceptance_timed_out"], "acceptance_timed_out"
            ),
            acceptance_integrity_failures=tuple(cast(list[str], failures_value)),
        )


@dataclass(frozen=True, slots=True)
class TerminalOutcomeReceipt:
    run_id: str
    trajectory_index: int
    session_id: str | None
    outcome: TerminalOutcome
    signals: OutcomeSignals
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported terminal outcome receipt version")
        if _RUN_ID_PATTERN.fullmatch(self.run_id) is None:
            raise ValueError("terminal outcome receipt has an unsafe run_id")
        if self.trajectory_index < 1:
            raise ValueError("terminal outcome trajectory_index must be positive")
        if self.session_id is not None and not self.session_id:
            raise ValueError("terminal outcome session_id must be nonempty or null")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "trajectory_index": self.trajectory_index,
            "session_id": self.session_id,
            "outcome": self.outcome.as_dict(),
            "signals": self.signals.as_dict(),
        }

    @classmethod
    def from_mapping(cls, value: object) -> TerminalOutcomeReceipt:
        mapping = _mapping(value, "terminal outcome receipt")
        fields = {
            "schema_version",
            "run_id",
            "trajectory_index",
            "session_id",
            "outcome",
            "signals",
        }
        _exact_keys(mapping, fields, set(), "terminal outcome receipt")
        session_id = mapping["session_id"]
        if session_id is not None and not isinstance(session_id, str):
            raise ValueError("terminal outcome session_id must be a string or null")
        run_id = mapping["run_id"]
        if not isinstance(run_id, str):
            raise ValueError("terminal outcome run_id must be a string")
        return cls(
            schema_version=_integer(mapping["schema_version"], "schema_version"),
            run_id=run_id,
            trajectory_index=_integer(
                mapping["trajectory_index"], "trajectory_index"
            ),
            session_id=session_id,
            outcome=TerminalOutcome.from_mapping(mapping["outcome"]),
            signals=OutcomeSignals.from_mapping(mapping["signals"]),
        )


def classify_terminal_outcome(
    signals: OutcomeSignals,
    evidence: OutcomeEvidence,
    *,
    budget_fully_exhausted: bool = False,
    terminal_tool_error: bool = False,
) -> TerminalOutcome:
    if signals.agent_timed_out or signals.acceptance_timed_out:
        return TerminalOutcome(0, TerminationReason.TIMEOUT, evidence)
    if signals.acceptance_integrity_failures:
        return TerminalOutcome(None, TerminationReason.UNKNOWN, evidence)
    if signals.agent_exit_code != 0:
        return TerminalOutcome(0, TerminationReason.TOOL_ERROR, evidence)
    if signals.acceptance_configured:
        if signals.acceptance_exit_code is None:
            return TerminalOutcome(None, TerminationReason.UNKNOWN, evidence)
        if signals.acceptance_exit_code == 0:
            return TerminalOutcome(1, TerminationReason.SUCCESS, evidence)
        return TerminalOutcome(0, TerminationReason.FAILED_ASSERTION, evidence)
    if budget_fully_exhausted:
        return TerminalOutcome(None, TerminationReason.BUDGET_EXHAUSTED, evidence)
    if terminal_tool_error:
        return TerminalOutcome(None, TerminationReason.TOOL_ERROR, evidence)
    return TerminalOutcome(None, TerminationReason.UNKNOWN, evidence)


def session_terminal_outcome_receipt_path(session_jsonl: Path) -> Path:
    """Return the controller-to-daemon handoff beside the native transcript."""
    return session_jsonl.resolve().parent / ".synth-terminal-outcome.json"


def write_terminal_outcome_receipt(
    path: Path, receipt: TerminalOutcomeReceipt
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(receipt.as_dict(), indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.replace(path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)
        raise


def read_terminal_outcome_receipt(path: Path) -> TerminalOutcomeReceipt:
    try:
        value: object = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f"cannot read terminal outcome receipt {path}: {error}"
        ) from error
    return TerminalOutcomeReceipt.from_mapping(value)


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _exact_keys(
    mapping: Mapping[str, object],
    required: set[str],
    optional: set[str],
    label: str,
) -> None:
    missing = required - set(mapping)
    extra = set(mapping) - required - optional
    if missing or extra:
        raise ValueError(
            f"{label} keys are invalid: missing={sorted(missing)}, "
            f"extra={sorted(extra)}"
        )


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value
