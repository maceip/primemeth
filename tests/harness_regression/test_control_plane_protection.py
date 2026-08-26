from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from synth.control_plane import (
    ControlPlaneProtectionError,
    protect_control_plane,
    protected_control_plane,
    require_control_plane_protected,
)

_CONTROL_ROOTS = (
    Path("packages"),
    Path("prime-agent-runtime"),
    Path(".prime/agent/extensions"),
)


def test_protection_is_recursive_and_restores_exact_modes(tmp_path: Path) -> None:
    files = _make_control_plane(tmp_path)
    data_plane = tmp_path / ".prime" / "agent" / "skills" / "fixture.txt"
    data_plane.parent.mkdir(parents=True)
    data_plane.write_text("runtime data\n")
    os.chmod(data_plane, 0o640)
    original_modes = _modes(tmp_path)

    with protected_control_plane(tmp_path) as protection:
        assert protection.protected_roots == tuple(
            path.as_posix() for path in _CONTROL_ROOTS
        )
        assert require_control_plane_protected(tmp_path) == protection.protected_roots
        for path in _control_nodes(tmp_path):
            assert stat.S_IMODE(path.lstat().st_mode) & 0o222 == 0
        assert stat.S_IMODE(data_plane.stat().st_mode) == 0o640
        if os.geteuid() != 0:
            with pytest.raises(PermissionError):
                files[0].write_text("blocked\n")

    assert _modes(tmp_path) == original_modes
    protection.restore()
    assert _modes(tmp_path) == original_modes


def test_exception_path_restores_modes(tmp_path: Path) -> None:
    _make_control_plane(tmp_path)
    original_modes = _modes(tmp_path)

    with (
        pytest.raises(RuntimeError, match="fixture failure"),
        protected_control_plane(tmp_path),
    ):
        raise RuntimeError("fixture failure")

    assert _modes(tmp_path) == original_modes


def test_symlink_rejection_rolls_back_already_protected_roots(
    tmp_path: Path,
) -> None:
    _make_control_plane(tmp_path)
    link = tmp_path / "prime-agent-runtime" / "nested" / "external-link"
    link.symlink_to(tmp_path / "outside")
    original_modes = _modes(tmp_path)

    with pytest.raises(ControlPlaneProtectionError, match="contains a symlink"):
        protect_control_plane(tmp_path)

    assert _modes(tmp_path) == original_modes


def test_chmod_failure_rolls_back_partial_protection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _make_control_plane(tmp_path)
    original_modes = _modes(tmp_path)
    failure_path = files[1]
    real_chmod = os.chmod
    failure_raised = False

    def flaky_chmod(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        mode: int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal failure_raised
        if Path(path) == failure_path and not failure_raised and mode & 0o222 == 0:
            failure_raised = True
            raise PermissionError("injected chmod failure")
        real_chmod(
            path,
            mode,
            dir_fd=dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "chmod", flaky_chmod)
    with pytest.raises(ControlPlaneProtectionError, match="could not protect"):
        protect_control_plane(tmp_path)

    assert failure_raised is True
    assert _modes(tmp_path) == original_modes


def _make_control_plane(root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for index, relative in enumerate(_CONTROL_ROOTS):
        nested = root / relative / "nested"
        nested.mkdir(parents=True)
        os.chmod(root / relative, 0o750 + index)
        os.chmod(nested, 0o751 + index)
        file_path = nested / "control.txt"
        file_path.write_text("control\n")
        os.chmod(file_path, 0o640 + index)
        files.append(file_path)
    return tuple(files)


def _control_nodes(root: Path) -> tuple[Path, ...]:
    return tuple(
        path
        for relative in _CONTROL_ROOTS
        for path in (root / relative, *(root / relative).rglob("*"))
    )


def _modes(root: Path) -> dict[str, int]:
    return {
        str(path.relative_to(root)): stat.S_IMODE(path.lstat().st_mode)
        for path in _control_nodes(root)
    }
