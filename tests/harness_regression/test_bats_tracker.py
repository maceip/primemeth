from __future__ import annotations

from middleware.bats_tracker import (
    BudgetAmounts,
    BudgetRegime,
    regime_for,
    regime_for_fractions,
)


def test_regime_boundaries_and_unlimited_limits() -> None:
    cases = (
        (1.0, BudgetRegime.HIGH),
        (0.7, BudgetRegime.HIGH),
        (0.699, BudgetRegime.MEDIUM),
        (0.3, BudgetRegime.MEDIUM),
        (0.299, BudgetRegime.LOW),
        (0.1, BudgetRegime.LOW),
        (0.099, BudgetRegime.CRITICAL),
        (0.0, BudgetRegime.CRITICAL),
    )
    assert [regime_for_fractions((fraction,)) for fraction, _ in cases] == [
        expected for _, expected in cases
    ]
    amounts = BudgetAmounts(
        limits={"bash": 10, "edit": 0, "tokens": 0},
        used={"bash": 4, "edit": 100, "tokens": 100_000},
    )
    assert amounts.remaining_fractions() == (0.6,)
    assert regime_for(amounts) is BudgetRegime.MEDIUM
    assert regime_for(BudgetAmounts(limits={}, used={})) is BudgetRegime.HIGH
