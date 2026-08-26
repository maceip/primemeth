"""Trusted kernel affordance for scheduling Synth skill compilation."""

from __future__ import annotations

import json
import re

from refine import run as schedule_refinement

MARKER_PREFIX = "SYNTH_SKILL_COMPILE_V1:"
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _marker(workflow: str, name_hint: str | None, reason: str) -> str:
    normalized_workflow = workflow.strip()
    normalized_reason = reason.strip()
    if len(normalized_workflow) < 20 or len(normalized_workflow) > 8_000:
        raise ValueError("workflow must contain 20 to 8000 characters")
    if not normalized_reason or len(normalized_reason) > 1_000:
        raise ValueError("reason must contain 1 to 1000 characters")
    normalized_name = name_hint.strip() if name_hint else None
    if normalized_name is not None and (
        len(normalized_name) > 64 or _NAME_RE.fullmatch(normalized_name) is None
    ):
        raise ValueError("name_hint must be lowercase kebab-case")
    return MARKER_PREFIX + json.dumps(
        {
            **({} if normalized_name is None else {"nameHint": normalized_name}),
            "reason": normalized_reason,
            "source": "kernel",
            "workflow": normalized_workflow,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


async def run(
    workflow: str,
    *,
    name_hint: str | None = None,
    reason: str = "Recurring workflow selected from the active session",
) -> dict[str, object]:
    """Schedule a compiler request through the native turn-end refinement seam."""

    marker = _marker(workflow, name_hint, reason)
    result = await schedule_refinement(marker)
    return {str(key): value for key, value in result.items()}
