from words import is_palindrome


def test_palindrome_predicate() -> None:
    assert is_palindrome("Level")
    assert not is_palindrome("Synth")
