"""BATS candidate selector.

wired into the synth CLI at Phase 5 integration (orphan-check exemption)
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypedDict

from middleware.batch_driver import VerificationPayload, parse_verification

# Source: budget-aware-agent/src/select_answer.py:72 at 37d23f6.
MAX_CANDIDATES = 10

SelectionMetrics = TypedDict(
    "SelectionMetrics",
    {"pass": float, "select": float, "first": float},
)


@dataclass(frozen=True)
class Candidate:
    number: int
    path: Path
    diff: str
    verification: VerificationPayload


@dataclass(frozen=True)
class JudgeRequest:
    run_dir: Path
    candidates: tuple[Candidate, ...]


@dataclass(frozen=True)
class JudgeResult:
    scores: tuple[float, ...]
    selected_index: int | None
    response: str


class Judge(Protocol):
    def __call__(self, request: JudgeRequest, /) -> JudgeResult: ...


@dataclass(frozen=True)
class SelectionComputation:
    selected_index: int
    metrics: SelectionMetrics


@dataclass(frozen=True)
class SelectionResult:
    path: Path
    selected_index: int
    selected_candidate: int | None
    metrics: SelectionMetrics


def compute_selection_metrics(
    scores: tuple[float, ...], proposed_selected_index: int | None
) -> SelectionComputation:
    """Port ``select_answer.py`` pass/select/first scoring semantics."""
    if not scores:
        raise ValueError("at least one candidate score is required")
    if any(not math.isfinite(score) or score not in (0.0, 1.0) for score in scores):
        raise ValueError("candidate scores must be binary finite values")

    positive_count = sum(score > 0 for score in scores)
    pass_score = 1.0 if positive_count > 0 else 0.0
    if pass_score == 0:
        selected_index = -1
        select_score = 0.0
    elif positive_count == len(scores):
        selected_index = 0
        select_score = 1.0
    else:
        if proposed_selected_index is None or not 0 <= proposed_selected_index < len(
            scores
        ):
            raise ValueError("judge selected index is missing or out of range")
        selected_index = proposed_selected_index
        select_score = scores[selected_index]

    return SelectionComputation(
        selected_index=selected_index,
        metrics={
            "pass": pass_score,
            "select": select_score,
            "first": scores[0],
        },
    )


def select_candidates(run_dir: Path, judge: Judge) -> SelectionResult:
    """Judge verified candidate diffs and emit ``selection.json``."""
    resolved_run_dir = run_dir.resolve(strict=True)
    candidates = _load_candidates(resolved_run_dir)
    if not candidates:
        raise ValueError(f"no candidates found under {resolved_run_dir}")

    judge_result = judge(
        JudgeRequest(run_dir=resolved_run_dir, candidates=tuple(candidates))
    )
    if len(judge_result.scores) != len(candidates):
        raise ValueError("judge must score every candidate")
    computation = compute_selection_metrics(
        judge_result.scores, judge_result.selected_index
    )
    selected_candidate = (
        candidates[computation.selected_index].number
        if computation.selected_index >= 0
        else None
    )
    selection_path = resolved_run_dir / "selection.json"
    selection_path.write_text(
        json.dumps(
            {
                "run_id": resolved_run_dir.name,
                "candidate_count": len(candidates),
                "selected_index": computation.selected_index,
                "selected_candidate": selected_candidate,
                "scores": list(judge_result.scores),
                "metrics": computation.metrics,
                "judge_response": judge_result.response,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return SelectionResult(
        path=selection_path,
        selected_index=computation.selected_index,
        selected_candidate=selected_candidate,
        metrics=computation.metrics,
    )


def _load_candidates(run_dir: Path) -> list[Candidate]:
    candidates_dir = run_dir / "candidates"
    if not candidates_dir.is_dir():
        return []
    candidate_dirs = sorted(
        (
            path
            for path in candidates_dir.iterdir()
            if path.is_dir() and path.name.isdigit()
        ),
        key=lambda path: int(path.name),
    )[:MAX_CANDIDATES]

    candidates: list[Candidate] = []
    for candidate_dir in candidate_dirs:
        diff_path = candidate_dir / "diff.patch"
        verification_path = candidate_dir / "verification.json"
        if not diff_path.is_file() or not verification_path.is_file():
            raise ValueError(f"incomplete candidate artifact: {candidate_dir}")
        verification = parse_verification(verification_path.read_text())
        if verification["decision"] != "SUCCESS":
            raise ValueError(f"candidate was not verification-gated: {candidate_dir}")
        candidates.append(
            Candidate(
                number=int(candidate_dir.name),
                path=candidate_dir,
                diff=diff_path.read_text(),
                verification=verification,
            )
        )
    return candidates
