def backoff(base_seconds: float, attempt: int) -> float:
    return base_seconds * attempt
