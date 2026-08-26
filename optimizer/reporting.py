from __future__ import annotations

from dataclasses import dataclass

from optimizer.vendor.autosaddler_v2.core.domain import Evaluation


@dataclass(frozen=True, slots=True)
class GateRates:
    fix_rate: float
    regression_rate: float
    fixed: int
    baseline_failures: int
    regressed: int
    baseline_passes: int


def compute_gate_rates(baseline: Evaluation, candidate: Evaluation) -> GateRates:
    if baseline.split != "development" or candidate.split != "development":
        raise ValueError("Gate rates require development evaluations")
    if baseline.requested_case_ids != candidate.requested_case_ids:
        raise ValueError("Gate rates require the same ordered development cases")
    before = _case_passes(baseline)
    after = _case_passes(candidate)
    baseline_failures = sum(not passed for passed in before.values())
    baseline_passes = sum(before.values())
    fixed = sum(not before[case_id] and after[case_id] for case_id in before)
    regressed = sum(before[case_id] and not after[case_id] for case_id in before)
    return GateRates(
        fix_rate=fixed / baseline_failures if baseline_failures else 0.0,
        regression_rate=regressed / baseline_passes if baseline_passes else 0.0,
        fixed=fixed,
        baseline_failures=baseline_failures,
        regressed=regressed,
        baseline_passes=baseline_passes,
    )


def _case_passes(evaluation: Evaluation) -> dict[str, bool]:
    result: dict[str, bool] = {}
    for case_id in evaluation.requested_case_ids:
        observations = [
            item for item in evaluation.observations if item.case_id == case_id
        ]
        if not observations or any(not item.is_valid for item in observations):
            raise ValueError(
                f"Development case has no complete valid observations: {case_id}"
            )
        result[case_id] = all(
            item.disposition == "success" and item.score is not None and item.score > 0
            for item in observations
        )
    return result
