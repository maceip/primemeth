from slug import slugify


def test_slugifies_words() -> None:
    assert slugify("Synth Harness") == "synth-harness"
