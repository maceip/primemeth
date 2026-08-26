from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from optimizer.transport import StubPrimeAgentTransport
from optimizer.vendor.autosaddler_v2.core.engine import (
    _prepare_session_attempt_workspace,
)
from optimizer.vendor.autosaddler_v2.core.ports import MutationContext
from optimizer.vendor.autosaddler_v2.harness.git import GitHarnessSpace
from optimizer.vendor.autosaddler_v2.providers.workspace_renderer import RenderedSession
from synth.control_plane import (
    make_control_plane_removable,
    protect_control_plane,
    require_control_plane_protected,
)

_CONTROL_ROOTS = (
    Path("packages"),
    Path("prime-agent-runtime"),
    Path(".prime/agent/extensions"),
)


def test_optimizer_worktrees_are_protected_and_cleaned_up(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    control_files = _initialize_repo(repo)
    source_modes = {path: path.stat().st_mode for path in control_files}
    worktrees_before = _worktree_listing(repo)
    harness = GitHarnessSpace(
        source_repo=repo,
        base_revision="HEAD",
        store_root=tmp_path / "store" / "candidates",
        worktree_root=tmp_path / "materialized",
        writable_paths=(PurePosixPath(".prime/agent/skills"),),
    )
    seed = harness.seed()

    mutation = harness.begin_mutation(
        seed,
        MutationContext(
            iteration=0,
            patch_label="capability",
            evidence=None,
            workspace_root=tmp_path / "mutations",
        ),
    )
    assert require_control_plane_protected(mutation.workspace) == tuple(
        path.as_posix() for path in _CONTROL_ROOTS
    )
    _assert_read_only(mutation.workspace)
    skill = mutation.workspace / ".prime/agent/skills/example/SKILL.md"
    skill.write_text("updated data plane\n")

    materialized = harness.materialize(seed, "inspect")
    assert require_control_plane_protected(materialized.root) == tuple(
        path.as_posix() for path in _CONTROL_ROOTS
    )
    _assert_read_only(materialized.root)
    materialized_path = materialized.root
    materialized.release()
    assert not materialized_path.exists()

    harness.cleanup_managed_worktrees()
    assert not mutation.workspace.exists()
    assert _worktree_listing(repo) == worktrees_before
    assert {path: path.stat().st_mode for path in control_files} == source_modes


def test_retry_replaces_inherited_read_only_attempt_workspace(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _make_control_plane(source)
    destination = tmp_path / "attempt"
    protection = protect_control_plane(source)
    try:
        _prepare_session_attempt_workspace(source, destination)
        assert require_control_plane_protected(destination) == tuple(
            path.as_posix() for path in _CONTROL_ROOTS
        )
        _assert_read_only(destination)
        _prepare_session_attempt_workspace(source, destination)
        assert require_control_plane_protected(destination) == tuple(
            path.as_posix() for path in _CONTROL_ROOTS
        )
    finally:
        protection.restore()
        make_control_plane_removable(destination)


def test_stub_requires_full_protection_only_for_diagnosis(tmp_path: Path) -> None:
    transport = StubPrimeAgentTransport(
        target_path=".prime/agent/skills/example/SKILL.md",
        improved_text="improved\n",
    )
    evolve = _rendered_session(
        tmp_path / "evolve",
        {"session_kind": "evolve", "candidate_ids": ["candidate-1"]},
    )
    outcome = asyncio.run(transport.run(evolve, 1.0))
    assert outcome.structured_output is not None

    diagnose_root = tmp_path / "diagnose"
    diagnose_root.mkdir()
    _make_control_plane(diagnose_root)
    diagnose = _rendered_session(
        diagnose_root,
        {"session_kind": "diagnose_patch"},
    )
    with pytest.raises(RuntimeError, match="remain writable"):
        asyncio.run(transport.run(diagnose, 1.0))

    protection = protect_control_plane(diagnose_root)
    try:
        outcome = asyncio.run(transport.run(diagnose, 1.0))
        assert outcome.structured_output is not None
    finally:
        protection.restore()


def _initialize_repo(repo: Path) -> tuple[Path, ...]:
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    control_files = _make_control_plane(repo)
    skill = repo / ".prime/agent/skills/example/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("seed data plane\n")
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "add",
            "packages",
            "prime-agent-runtime",
            ".prime",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Synth Fixture",
            "-c",
            "user.email=synth@example.invalid",
            "commit",
            "-q",
            "-m",
            "base",
        ],
        check=True,
    )
    return control_files


def _make_control_plane(root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for relative in _CONTROL_ROOTS:
        path = root / relative / "example" / "control.txt"
        path.parent.mkdir(parents=True)
        path.write_text("control\n")
        os.chmod(path, 0o644)
        files.append(path)
    return tuple(files)


def _worktree_listing(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _rendered_session(
    workspace: Path,
    context: dict[str, object],
) -> RenderedSession:
    autosaddler = workspace / ".autosaddler"
    autosaddler.mkdir(parents=True, exist_ok=True)
    context_path = autosaddler / "session_context.json"
    context_path.write_text(json.dumps(context))
    instruction_path = autosaddler / "instructions.md"
    instruction_path.write_text("fixture\n")
    return RenderedSession(
        provider="stub",
        workspace=workspace,
        task_prompt="fixture",
        system_context="fixture",
        instruction_path=instruction_path,
        skill_directory=autosaddler / "skills",
        output_schema_path=autosaddler / "schema.json",
        output_path=autosaddler / "output.json",
        allowed_tools=(),
    )


def _assert_read_only(root: Path) -> None:
    for relative in _CONTROL_ROOTS:
        for path in (root / relative, *(root / relative).rglob("*")):
            assert stat.S_IMODE(path.lstat().st_mode) & 0o222 == 0
