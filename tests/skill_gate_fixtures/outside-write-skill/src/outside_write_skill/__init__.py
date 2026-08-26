from pathlib import Path

MARKER = Path("/var/lib/synth-skill-gate/imported.txt")
MARKER.write_text("unsafe")


def run(text: str) -> str:
    """Return text after the module records its marker."""
    return text
