from __future__ import annotations

import pytest

from middleware.batch_select import compute_selection_metrics


def test_metrics_when_no_candidate_passes() -> None:
    result = compute_selection_metrics((0.0, 0.0), proposed_selected_index=1)
    assert result.selected_index == -1
    assert result.metrics == {"pass": 0.0, "select": 0.0, "first": 0.0}


def test_metrics_when_all_candidates_pass_skip_selection() -> None:
    result = compute_selection_metrics((1.0, 1.0), proposed_selected_index=1)
    assert result.selected_index == 0
    assert result.metrics == {"pass": 1.0, "select": 1.0, "first": 1.0}


def test_metrics_use_judge_choice_when_only_some_candidates_pass() -> None:
    result = compute_selection_metrics((0.0, 1.0), proposed_selected_index=1)
    assert result.selected_index == 1
    assert result.metrics == {"pass": 1.0, "select": 1.0, "first": 0.0}


def test_metrics_reject_invalid_mixed_selection() -> None:
    with pytest.raises(ValueError, match="out of range"):
        compute_selection_metrics((0.0, 1.0), proposed_selected_index=2)
