def refine_skill(text: str) -> str:
    """Stand in for a forbidden refinement operation."""
    return text.strip()


def run(text: str) -> str:
    """Invoke the fixture's forbidden refinement operation."""
    return refine_skill(text)
