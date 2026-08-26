"""Phase ownership metadata for the independently developed optimizer."""

from synth.capability import CapabilityStatus

AUTOSADDLER_CAPABILITY = CapabilityStatus(
    name="autosaddler",
    available=False,
    planned_phase=6,
    detail="The reviewed synth/p6-autosaddler branch owns this capability.",
)
