from good_skill import run


def test_run_normalizes_text() -> None:
    assert run("  ready  ", uppercase=True) == "READY"


def test_run_preserves_case_by_default() -> None:
    assert run("  Ready  ") == "Ready"
