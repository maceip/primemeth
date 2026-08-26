"""Public construction helpers for the Phase 6 AutoSaddler integration."""

from optimizer.runtime import SynthRuntime, build_synth_runtime
from optimizer.vendor.autosaddler_v2.cli import main as autosaddler_v2_main
from synth.capability import CapabilityStatus

AUTOSADDLER_CAPABILITY = CapabilityStatus(
    name="autosaddler",
    available=True,
    planned_phase=6,
    detail="Reviewed AutoSaddler v2 engine and Synth harness scenario are installed.",
)

__all__ = [
    "AUTOSADDLER_CAPABILITY",
    "SynthRuntime",
    "autosaddler_v2_main",
    "build_synth_runtime",
]
