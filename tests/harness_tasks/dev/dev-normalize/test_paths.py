from paths import normalize


def test_collapses_repeated_separators() -> None:
    assert normalize("/a////b") == "/a/b"
