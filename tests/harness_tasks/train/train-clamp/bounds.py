def clamp(value: int, low: int, high: int) -> int:
    return max(high, min(low, value))
