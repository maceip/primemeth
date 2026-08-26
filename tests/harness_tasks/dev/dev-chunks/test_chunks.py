from chunks import chunks


def test_does_not_append_empty_chunk() -> None:
    assert chunks([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]
