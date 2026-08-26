"""Honest capability registry for implemented and phase-owned services."""

from __future__ import annotations

from middleware.bats_tracker import BATS_CAPABILITY
from optimizer.autosaddler import AUTOSADDLER_CAPABILITY
from synth.capability import CapabilityStatus

BRIDGE_CAPABILITY = CapabilityStatus(
    name="bridge",
    available=True,
    planned_phase=2,
    detail="Long-lived JSON-lines bridge with local embedding support.",
)
TELEMETRY_CAPABILITY = CapabilityStatus(
    name="telemetry",
    available=True,
    planned_phase=2,
    detail="Extension-owned JSONL telemetry with Python validation.",
)
REASONING_BANK_CAPABILITY = CapabilityStatus(
    name="reasoning_bank",
    available=False,
    planned_phase=4,
    detail=(
        "Wire contracts exist; storage, retrieval, judging, and distillation land "
        "in Phase 4."
    ),
)

CAPABILITIES = (
    BRIDGE_CAPABILITY,
    TELEMETRY_CAPABILITY,
    BATS_CAPABILITY,
    REASONING_BANK_CAPABILITY,
    AUTOSADDLER_CAPABILITY,
)
