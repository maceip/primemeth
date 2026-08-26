"""Shared capability status reported by the Synth CLI."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CapabilityStatus:
    name: str
    available: bool
    planned_phase: int
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "available": self.available,
            "plannedPhase": self.planned_phase,
            "detail": self.detail,
        }
