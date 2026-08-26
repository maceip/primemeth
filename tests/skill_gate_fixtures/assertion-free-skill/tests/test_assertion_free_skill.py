from assertion_free_skill import run


def test_run_without_an_assertion() -> None:
    run(" value ")


def test_run_again_without_an_assertion() -> None:
    run("another value")
