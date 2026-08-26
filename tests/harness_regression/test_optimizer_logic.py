from __future__ import annotations

from optimizer.synth_harness import anti_cheating_scan
from optimizer.vendor.autosaddler_v2.core.domain import (
    ArtifactRef,
    Candidate,
    Evaluation,
    Observation,
)
from optimizer.vendor.autosaddler_v2.core.policies import (
    MatchedValidStrictImprovement,
    MeanDevelopmentRanking,
)


def test_acceptance_requires_strict_matched_improvement() -> None:
    parent = _evaluation("a", "train_before", (0.0, 1.0))
    improved = _evaluation("b", "train_after", (1.0, 1.0))
    unchanged = _evaluation("c", "train_after", (0.0, 1.0))

    policy = MatchedValidStrictImprovement()

    assert policy.compare(parent, improved).accepted is True
    assert policy.compare(parent, unchanged).accepted is False


def test_development_ranking_selects_highest_mean_score() -> None:
    first = _candidate("a")
    second = _candidate("b")
    first_evaluation = _evaluation("a", "development", (1.0, 0.0))
    second_evaluation = _evaluation("b", "development", (1.0, 1.0))

    selected, evaluation = MeanDevelopmentRanking().select(
        ((first, first_evaluation), (second, second_evaluation))
    )

    assert selected == second
    assert evaluation.aggregate_score == 1.0


def test_anti_cheating_scan_checks_added_lines_only() -> None:
    patch = """\
--- a/skill.md
+++ b/skill.md
- prior note mentions dev-secret
+ repair train-secret directly
 context dev-secret
"""

    assert anti_cheating_scan(patch, ("train-secret", "dev-secret")) == (
        "train-secret",
    )


def _candidate(suffix: str) -> Candidate:
    candidate_id = f"sha256:{suffix * 64}"
    return Candidate(
        candidate_id=candidate_id,
        parent_ids=(),
        space="test",
        artifact=ArtifactRef(uri=f"candidates/{suffix}.json", kind="test"),
    )


def _evaluation(
    suffix: str,
    purpose: str,
    scores: tuple[float, ...],
) -> Evaluation:
    candidate = _candidate(suffix)
    split = "development" if purpose == "development" else "train"
    observations = tuple(
        Observation.create(
            candidate_id=candidate.candidate_id,
            case_id=f"case-{index}",
            split=split,  # type: ignore[arg-type]
            repetition=0,
            disposition="success" if score else "task_failure",
            score=score,
            evaluator_fingerprint="test/v1",
        )
        for index, score in enumerate(scores)
    )
    return Evaluation(
        evaluation_id=f"evaluation-{suffix}",
        candidate_id=candidate.candidate_id,
        split=split,  # type: ignore[arg-type]
        purpose=purpose,  # type: ignore[arg-type]
        iteration=0,
        requested_case_ids=tuple(item.case_id for item in observations),
        observations=observations,
        artifact_dir=ArtifactRef(uri=f"evaluations/{suffix}", kind="test"),
    )
