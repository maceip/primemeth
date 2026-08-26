from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from optimizer.reporting import GateRates, compute_gate_rates
from optimizer.runtime import SynthRuntime, build_synth_runtime
from optimizer.vendor.autosaddler_v2.core.domain import Evaluation, canonical_json
from optimizer.vendor.autosaddler_v2.core.run_state import RunState
from optimizer.vendor.autosaddler_v2.harness.git import GitHarnessSpace

DEFAULT_CONFIG = Path(__file__).resolve().parent / "configs" / "synth_local.yaml"
ZERO_OID = "0" * 40
OPTIMIZER_BRANCH = re.compile(r"^synth/optimizer/[A-Za-z0-9._-]+$")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="synth")
    subparsers = parser.add_subparsers(dest="command", required=True)
    optimize = subparsers.add_parser("optimize", help="run the AutoSaddler outer loop")
    optimize.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    optimize.add_argument("--run-id")
    optimize.add_argument("--branch")
    arguments = parser.parse_args(argv)
    if arguments.command != "optimize":
        parser.error(f"unknown command: {arguments.command}")
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = arguments.run_id or f"synth-local-{timestamp}"
    branch = arguments.branch or f"synth/optimizer/{timestamp}"
    report = run_optimizer(
        config_path=arguments.config,
        run_id=run_id,
        branch=branch,
    )
    print(f"run_id={report['run_id']}")
    print(f"optimizer_branch={report['optimizer_branch']}")
    print(f"selected_candidate_id={report['selected_candidate_id']}")
    print(f"development_score={report['development_score']:.6f}")
    print(f"fix_rate={report['fix_rate']:.6f}")
    print(f"regression_rate={report['regression_rate']:.6f}")
    print(f"gated_commit={report['gated_commit']}")
    print(f"report={report['report_path']}")


def run_optimizer(
    *,
    config_path: Path,
    run_id: str,
    branch: str,
) -> dict[str, object]:
    if not OPTIMIZER_BRANCH.fullmatch(branch):
        raise ValueError("Optimizer branch must match synth/optimizer/<name>")
    repo = _repo_root()
    starting_branch = _git(repo, "branch", "--show-current")
    _require_clean(repo)
    base_revision = _git(repo, "rev-parse", "HEAD")
    runtime = build_synth_runtime(config_path, run_id=run_id)
    harness = cast(GitHarnessSpace, runtime.scenario.harness_space)
    harness_repo = harness.source_repo
    if harness_repo != repo:
        raise ValueError(
            "Synth optimizer must use the current repository as its Git harness"
        )
    if harness.base_revision != base_revision:
        raise ValueError(
            "Synth optimizer base revision must resolve to the clean current HEAD"
        )
    branch_ref = f"refs/heads/{branch}"
    _git(repo, "update-ref", branch_ref, base_revision, ZERO_OID)

    result = runtime.engine.run()
    state = RunState.replay(runtime.store.events())
    seed_id = state.accepted_candidate_ids[0]
    baseline_development = _development_evaluation(state, seed_id)
    selected_development = _development_evaluation(
        state,
        result.selected_candidate_id,
    )
    rates = compute_gate_rates(baseline_development, selected_development)
    gated_commit = base_revision
    if result.selected_candidate_id != seed_id:
        selected_revision = _candidate_revision(runtime, result.selected_candidate_id)
        gated_commit = _create_gated_commit(
            repo=repo,
            base_revision=base_revision,
            selected_revision=selected_revision,
            run_id=run_id,
            candidate_id=result.selected_candidate_id,
            development_score=result.development_score,
            rates=rates,
        )
        _git(repo, "update-ref", branch_ref, gated_commit, base_revision)

    report_path = runtime.store.run_dir / "optimizer-report.json"
    report: dict[str, object] = {
        "schema_version": "synth-optimizer-report/v1",
        "run_id": run_id,
        "optimizer_branch": branch,
        "base_revision": base_revision,
        "selected_candidate_id": result.selected_candidate_id,
        "development_score": result.development_score,
        **asdict(rates),
        "gated_commit": gated_commit,
        "report_path": str(report_path),
    }
    report_path.write_text(canonical_json(report) + "\n", encoding="utf-8")
    if _git(repo, "branch", "--show-current") != starting_branch:
        raise RuntimeError("Optimizer changed the current worktree branch")
    _require_clean(repo)
    return report


def _repo_root() -> Path:
    return Path(_git(Path.cwd(), "rev-parse", "--show-toplevel")).resolve()


def _require_clean(repo: Path) -> None:
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise RuntimeError(f"Synth optimizer requires a clean worktree:\n{status}")


def _development_evaluation(state: RunState, candidate_id: str) -> Evaluation:
    evaluations = [
        evaluation
        for evaluation in state.evaluations.values()
        if evaluation.candidate_id == candidate_id and evaluation.split == "development"
    ]
    if not evaluations:
        raise ValueError(f"Candidate has no development evaluation: {candidate_id}")
    return evaluations[-1]


def _candidate_revision(runtime: SynthRuntime, candidate_id: str) -> str:
    manifest_path = (
        runtime.store.run_dir
        / "candidates"
        / candidate_id.removeprefix("sha256:")
        / "candidate.json"
    )
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("revision"), str):
        raise TypeError(f"Candidate manifest is malformed: {manifest_path}")
    return cast(str, value["revision"])


def _create_gated_commit(
    *,
    repo: Path,
    base_revision: str,
    selected_revision: str,
    run_id: str,
    candidate_id: str,
    development_score: float,
    rates: GateRates,
) -> str:
    tree = _git(repo, "rev-parse", f"{selected_revision}^{{tree}}")
    base_tree = _git(repo, "rev-parse", f"{base_revision}^{{tree}}")
    if tree == base_tree:
        raise ValueError("Accepted optimizer candidate has no tree changes")
    message = (
        "optimize(synth): gate AutoSaddler candidate\n\n"
        f"Run: {run_id}\n"
        f"Candidate: {candidate_id}\n"
        f"Development score: {development_score:.6f}\n"
        f"Fix rate: {rates.fix_rate:.6f}\n"
        f"Regression rate: {rates.regression_rate:.6f}\n"
    )
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_NAME": "Synth AutoSaddler",
            "GIT_AUTHOR_EMAIL": "autosaddler@local",
            "GIT_COMMITTER_NAME": "Synth AutoSaddler",
            "GIT_COMMITTER_EMAIL": "autosaddler@local",
        }
    )
    completed = subprocess.run(
        ["git", "commit-tree", tree, "-p", base_revision],
        cwd=repo,
        env=environment,
        input=message,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


if __name__ == "__main__":
    main()
