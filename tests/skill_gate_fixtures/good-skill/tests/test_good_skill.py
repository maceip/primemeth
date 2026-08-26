from good_skill import run


def test_run_normalizes_text() -> None:
    assert run("  ready  ", uppercase=True) == "READY"
