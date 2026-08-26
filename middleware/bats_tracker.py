"""Phase ownership metadata for the pending BATS runtime capability."""

from synth.capability import CapabilityStatus

BATS_CAPABILITY = CapabilityStatus(
    name="bats",
    available=False,
    planned_phase=3,
    detail="Budget tracking is configured in Phase 2 and enforced in Phase 3.",
)
