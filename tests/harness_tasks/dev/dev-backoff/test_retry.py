from retry import backoff


def test_doubles_delay_each_attempt() -> None:
    assert backoff(0.5, 0) == 0.5
    assert backoff(0.5, 3) == 4.0
