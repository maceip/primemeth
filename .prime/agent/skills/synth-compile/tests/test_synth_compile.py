from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

from synth_compile import MARKER_PREFIX, run


def test_run_schedules_a_strict_marker() -> None:
    scheduler = AsyncMock(return_value={"scheduled": True})
    with patch("synth_compile.schedule_refinement", scheduler):
        result = asyncio.run(
            run(
                "Read the focused test, repair the implementation, and rerun the test.",
                name_hint="focused-repair",
                reason="Repeated verified workflow",
            )
        )

    assert result == {"scheduled": True}
    marker = scheduler.await_args.args[0]
    assert marker.startswith(MARKER_PREFIX)
    payload = json.loads(marker.removeprefix(MARKER_PREFIX))
    assert payload["nameHint"] == "focused-repair"


def test_run_rejects_a_trivial_workflow() -> None:
    try:
        asyncio.run(run("one step"))
    except ValueError as error:
        assert "20 to 8000" in str(error)
    else:
        raise AssertionError("run() accepted a trivial workflow")


def test_run_omits_an_absent_name_hint() -> None:
    scheduler = AsyncMock(return_value={"scheduled": True})
    with patch("synth_compile.schedule_refinement", scheduler):
        asyncio.run(
            run("Read repeated evidence, normalize it, and verify the result.")
        )

    payload = json.loads(
        scheduler.await_args.args[0].removeprefix(MARKER_PREFIX)
    )
    assert "nameHint" not in payload
