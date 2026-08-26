from flags import parse_bool


def test_parses_boolean_strings() -> None:
    assert parse_bool("yes") is True
    assert parse_bool("false") is False
