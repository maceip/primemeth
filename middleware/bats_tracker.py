"""Pure BATS budget math mirrored for the Synth CLI and batch plane."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from middleware.telemetry import BudgetSnapshot, TelemetryEvent
from synth.capability import CapabilityStatus

BUDGET_KEYS = ("bash", "edit", "ipython", "subagents", "tokens", "costUsd")


class BudgetRegime(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class BudgetAmounts:
    limits: Mapping[str, float]
    used: Mapping[str, float]

    def remaining_fractions(self) -> tuple[float, ...]:
        fractions: list[float] = []
        for key in BUDGET_KEYS:
            limit = float(self.limits.get(key, 0))
            if limit == 0:
                continue
            used = max(0.0, float(self.used.get(key, 0)))
            fractions.append(max(0.0, limit - used) / limit)
        return tuple(fractions)


def regime_for_fractions(fractions: tuple[float, ...]) -> BudgetRegime:
    if not fractions:
        return BudgetRegime.HIGH
    minimum = min(fractions)
    if minimum <= 0:
        return BudgetRegime.CRITICAL
    if minimum >= 0.7:
        return BudgetRegime.HIGH
    if minimum >= 0.3:
        return BudgetRegime.MEDIUM
    if minimum >= 0.1:
        return BudgetRegime.LOW
    return BudgetRegime.CRITICAL


def regime_for(amounts: BudgetAmounts) -> BudgetRegime:
    return regime_for_fractions(amounts.remaining_fractions())


def latest_budget_snapshot(path: Path) -> BudgetSnapshot:
    latest: BudgetSnapshot | None = None
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            event = TelemetryEvent.from_json_line(line)
            if event.kind == "budget" and event.budget is not None:
                latest = event.budget
    if latest is None:
        raise ValueError(f"{path}: contains no budget snapshot")
    return latest


BATS_CAPABILITY = CapabilityStatus(
    name="bats",
    available=True,
    planned_phase=3,
    detail=(
        "Native-usage budgets, BATS regimes, tool admission, continuation, and "
        "observation hygiene are active through the Synth extension."
    ),
)
