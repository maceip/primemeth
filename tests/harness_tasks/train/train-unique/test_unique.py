from unique import count_unique


def test_counts_distinct_values() -> None:
    assert count_unique(["a", "b", "a"]) == 2
