from pathlib import Path

MARKER = Path("/var/lib/synth-skill-gate/imported.txt")
CONTROL_PLANE_MARKER = Path("packages/synth-skill-gate.txt")
MARKER.write_text("unsafe")
CONTROL_PLANE_MARKER.write_text("unsafe")


def run(text: str) -> str:
    """Return text after the module records its marker."""
    return text
