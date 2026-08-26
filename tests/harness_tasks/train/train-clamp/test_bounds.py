from bounds import clamp


def test_clamps_inside_and_outside_range() -> None:
    assert clamp(5, 0, 10) == 5
    assert clamp(-3, 0, 10) == 0
    assert clamp(14, 0, 10) == 10
