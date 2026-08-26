def run(text: str, *, uppercase: bool = False) -> str:
    """Normalize surrounding whitespace and optionally uppercase the text."""
    normalized = text.strip()
    return normalized.upper() if uppercase else normalized
