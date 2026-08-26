from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from compiler.gate import (
    GatePolicy,
    _assertion_scan,
    _package_shape_check,
    _parse_frontmatter_text,
    _static_safety_check,
    _tree_hash,
    content_hash,
)
from compiler.service import _remove_runtime_cache_artifacts

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "skill_gate_fixtures"
POLICY = GatePolicy(repository_root=ROOT)


def shape_and_safety(fixture_name: str) -> tuple[bool, tuple[str, ...]]:
    candidate = FIXTURES / fixture_name
    shape, context = _package_shape_check(candidate)
    assert shape.passed, shape.evidence
    safety = _static_safety_check(candidate, POLICY, context)
    return safety.passed, safety.evidence


def test_all_fixture_packages_have_valid_shape() -> None:
    fixture_names = sorted(path.name for path in FIXTURES.iterdir() if path.is_dir())

    assert fixture_names == [
        "assertion-free-skill",
        "good-skill",
        "network-skill",
        "outside-write-skill",
        "refine",
        "self-modifying-skill",
    ]
    for fixture_name in fixture_names:
        shape, _ = _package_shape_check(FIXTURES / fixture_name)
        assert shape.passed, (fixture_name, shape.evidence)


@pytest.mark.parametrize(
    ("fixture_name", "evidence_fragment"),
    [
        ("network-skill", "network-capable import 'socket'"),
        ("outside-write-skill", "write_text target is outside"),
        ("refine", "shadows a bundled skill"),
        ("self-modifying-skill", "invocation of 'refine_skill'"),
    ],
)
def test_static_rejection_fixtures_isolate_expected_class(
    fixture_name: str, evidence_fragment: str
) -> None:
    passed, evidence = shape_and_safety(fixture_name)

    assert not passed
    assert any(evidence_fragment in item for item in evidence), evidence
    if fixture_name == "outside-write-skill":
        assert any("protected Synth control plane" in item for item in evidence)
    if fixture_name == "self-modifying-skill":
        assert any("process_execution" in item for item in evidence)
        assert any("protected skill compiler state" in item for item in evidence)


def test_good_fixture_passes_static_safety() -> None:
    passed, evidence = shape_and_safety("good-skill")

    assert passed, evidence
    assert len(evidence) == 8


def test_assertion_scan_rejects_only_assertion_free_fixture() -> None:
    good_tests = tuple((FIXTURES / "good-skill" / "tests").glob("test_*.py"))
    bad_tests = tuple((FIXTURES / "assertion-free-skill" / "tests").glob("test_*.py"))

    assert _assertion_scan(good_tests) == (1, ())
    count, failures = _assertion_scan(bad_tests)
    assert count == 1
    assert failures == (
        "test_assertion_free_skill.py: assertion-free test file is forbidden",
    )


def test_frontmatter_and_content_hash_are_deterministic() -> None:
    frontmatter, error = _parse_frontmatter_text(
        "---\nname: sample\nnetwork-imports: [httpx, socket]\n---\n"
    )
    candidate = FIXTURES / "good-skill"

    assert error is None
    assert frontmatter["network-imports"] == ["httpx", "socket"]
    assert _tree_hash(candidate) == _tree_hash(candidate)


def test_runtime_cache_cleanup_restores_the_gated_source_hash(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "good-skill"
    shutil.copytree(FIXTURES / "good-skill", candidate)
    expected_hash = content_hash(candidate)
    package_cache = candidate / "src" / "good_skill" / "__pycache__"
    package_cache.mkdir()
    (package_cache / "__init__.cpython-311.pyc").write_bytes(b"runtime bytecode")
    test_cache = candidate / ".pytest_cache"
    test_cache.mkdir()
    (test_cache / "README.md").write_text("runtime cache", encoding="utf-8")

    removed = _remove_runtime_cache_artifacts(candidate)

    assert removed == (
        ".pytest_cache",
        "src/good_skill/__pycache__",
        "src/good_skill/__pycache__/__init__.cpython-311.pyc",
    )
    assert content_hash(candidate) == expected_hash
