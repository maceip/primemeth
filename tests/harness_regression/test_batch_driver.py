from __future__ import annotations

import pytest

from middleware.batch_driver import (
    BudgetCounterState,
    BudgetState,
    parse_budget_text,
    parse_verification,
    should_start_fresh_trajectory,
)


def _budget(remaining: float | None) -> BudgetState:
    counter = BudgetCounterState(used=1.0, remaining=remaining)
    unlimited = BudgetCounterState(used=0.0, remaining=None)
    return BudgetState(
        bash=counter,
        edit=unlimited,
        ipython=unlimited,
        subagents=unlimited,
        tokens=unlimited,
        cost_usd=unlimited,
    )


@pytest.mark.parametrize(
    ("remaining", "threshold", "expected"),
    [(4.1, 0.8, True), (4.0, 0.8, False), (0.0, 0.0, False), (None, 1.0, True)],
)
def test_fresh_trajectory_thresholds_are_strict(
    remaining: float | None, threshold: float, expected: bool
) -> None:
    assert should_start_fresh_trajectory(_budget(remaining), threshold) is expected


def test_parse_budget_text_uses_last_persisted_block() -> None:
    text = (
        "<budget>\n"
        "Bash Budget Used: 4, Bash Budget Remaining: 6, "
        "Edit Budget Used: 5, Edit Budget Remaining: 4, "
        "IPython Budget Used: 1, IPython Budget Remaining: unlimited, "
        "Subagent Budget Used: 2, Subagent Budget Remaining: 2, "
        "Token Budget Used: 30, Token Budget Remaining: unlimited, "
        "Cost USD Budget Used: 0.25, Cost USD Budget Remaining: 0.75\n"
        "Make the best use of the available resources.\n"
        "</budget>\n"
    )
    parsed = parse_budget_text(text)
    assert parsed is not None
    assert parsed.used_values() == {
        "bash": 4.0,
        "edit": 5.0,
        "ipython": 1.0,
        "subagents": 2.0,
        "tokens": 30.0,
        "costUsd": 0.25,
    }
    assert parsed.bounded_fractions() == (0.6, 4 / 9, 0.5, 0.75)


@pytest.mark.parametrize("decision", ["SUCCESS", "CONTINUE", "PIVOT"])
def test_parse_verification_accepts_contract_decisions(decision: str) -> None:
    parsed = parse_verification(
        {
            "verification": "checked",
            "decision": decision,
            "justification": "fixture",
            "trajectory_summary": "summary",
            "details": {},
        }
    )
    assert parsed["decision"] == decision


def test_parse_verification_rejects_generation_without_verdict() -> None:
    with pytest.raises(ValueError, match="exactly"):
        parse_verification({"answer": "looks good"})
