from __future__ import annotations

import pytest

from middleware.batch_driver import (
    BudgetState,
    parse_budget_text,
    parse_verification,
    should_start_fresh_trajectory,
)


@pytest.mark.parametrize(
    ("query_remaining", "file_remaining", "expected"),
    [(6, 4, True), (5, 4, False), (6, 3, False), (5, 3, False)],
)
def test_fresh_trajectory_thresholds_are_strict(
    query_remaining: int, file_remaining: int, expected: bool
) -> None:
    budget = BudgetState(0, query_remaining, 0, file_remaining)
    assert should_start_fresh_trajectory(budget) is expected


def test_parse_budget_text_uses_last_persisted_block() -> None:
    text = (
        "Query Budget Used: 1, Query Budget Remaining: 9, "
        "URL Budget Used: 2, URL Budget Remaining: 8\n"
        "Query Budget Used: 4, Query Budget Remaining: 6, "
        "File Budget Used: 5, File Budget Remaining: 4\n"
    )
    assert parse_budget_text(text) == BudgetState(4, 6, 5, 4)


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
