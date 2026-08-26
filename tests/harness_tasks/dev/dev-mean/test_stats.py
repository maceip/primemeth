from stats import mean


def test_returns_fractional_mean() -> None:
    assert mean([1.0, 2.0]) == 1.5
