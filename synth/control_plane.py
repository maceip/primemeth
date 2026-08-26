"""OS-assisted write protection for Synth-managed workspaces."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MANAGED_WORKSPACE_ENV = "SYNTH_MANAGED_WORKSPACE"
CONTROL_PLANE_PATHS = (
    PurePosixPath("packages"),
    PurePosixPath("prime-agent-runtime"),
    PurePosixPath(".prime/agent/extensions"),
)


class ControlPlaneProtectionError(RuntimeError):
    """Raised when a managed workspace cannot be protected safely."""


@dataclass(frozen=True, slots=True)
class _ModeReceipt:
    path: Path
    mode: int


@dataclass(slots=True)
class ControlPlaneProtection:
    root: Path
    protected_roots: tuple[str, ...]
    _receipts: tuple[_ModeReceipt, ...]
    _restored: bool = False

    def restore(self) -> None:
        if self._restored:
            return
        errors: list[str] = []
        for receipt in sorted(
            self._receipts,
            key=lambda item: len(item.path.parts),
        ):
            try:
                if receipt.path.is_symlink() or not receipt.path.exists():
                    errors.append(f"{receipt.path}: path changed before restoration")
                    continue
                os.chmod(receipt.path, receipt.mode, follow_symlinks=False)
            except OSError as error:
                errors.append(f"{receipt.path}: {error}")
        if errors:
            raise ControlPlaneProtectionError(
                "could not restore managed control-plane permissions: "
                + "; ".join(errors)
            )
        self._restored = True


def protect_control_plane(root: Path) -> ControlPlaneProtection:
    resolved_root = root.expanduser().resolve(strict=True)
    receipts: list[_ModeReceipt] = []
    protected_roots: list[str] = []
    try:
        for relative in CONTROL_PLANE_PATHS:
            _reject_symlink_components(resolved_root, relative)
            target = resolved_root.joinpath(*relative.parts)
            if not target.exists():
                continue
            if target.is_symlink() or not target.is_dir():
                raise ControlPlaneProtectionError(
                    f"managed control-plane root must be a real directory: {relative}"
                )
            protected_roots.append(relative.as_posix())
            for path in _tree_nodes(target):
                mode = stat.S_IMODE(path.lstat().st_mode)
                receipts.append(_ModeReceipt(path, mode))
                os.chmod(path, mode & ~0o222, follow_symlinks=False)
    except Exception as error:
        rollback = ControlPlaneProtection(
            root=resolved_root,
            protected_roots=tuple(protected_roots),
            _receipts=tuple(receipts),
        )
        try:
            rollback.restore()
        except ControlPlaneProtectionError as rollback_error:
            raise ControlPlaneProtectionError(
                f"control-plane protection failed ({error}); rollback failed: "
                f"{rollback_error}"
            ) from error
        if isinstance(error, ControlPlaneProtectionError):
            raise
        raise ControlPlaneProtectionError(
            f"could not protect managed control plane: {error}"
        ) from error
    return ControlPlaneProtection(
        root=resolved_root,
        protected_roots=tuple(protected_roots),
        _receipts=tuple(receipts),
    )


@contextmanager
def protected_control_plane(root: Path) -> Iterator[ControlPlaneProtection]:
    protection = protect_control_plane(root)
    try:
        yield protection
    finally:
        protection.restore()


def writable_control_plane_roots(root: Path) -> tuple[str, ...]:
    resolved_root = root.expanduser().resolve(strict=True)
    writable: list[str] = []
    for relative in CONTROL_PLANE_PATHS:
        try:
            _reject_symlink_components(resolved_root, relative)
        except ControlPlaneProtectionError:
            writable.append(relative.as_posix())
            continue
        target = resolved_root.joinpath(*relative.parts)
        if not target.exists():
            continue
        if target.is_symlink() or not target.is_dir():
            writable.append(relative.as_posix())
            continue
        if any(
            stat.S_IMODE(path.lstat().st_mode) & 0o222
            for path in _tree_nodes(target)
        ):
            writable.append(relative.as_posix())
    return tuple(writable)


def require_control_plane_protected(root: Path) -> tuple[str, ...]:
    resolved_root = root.expanduser().resolve(strict=True)
    writable = writable_control_plane_roots(resolved_root)
    if writable:
        raise ControlPlaneProtectionError(
            "managed control-plane paths remain writable: " + ", ".join(writable)
        )
    return tuple(
        relative.as_posix()
        for relative in CONTROL_PLANE_PATHS
        if resolved_root.joinpath(*relative.parts).is_dir()
    )


def make_control_plane_removable(root: Path) -> None:
    resolved_root = root.expanduser().resolve()
    if not resolved_root.is_dir():
        return
    for relative in CONTROL_PLANE_PATHS:
        try:
            _reject_symlink_components(resolved_root, relative)
        except ControlPlaneProtectionError:
            continue
        target = resolved_root.joinpath(*relative.parts)
        if target.is_symlink() or not target.is_dir():
            continue
        for path in _removable_tree_nodes(target):
            mode = stat.S_IMODE(path.lstat().st_mode)
            os.chmod(path, mode | stat.S_IWUSR, follow_symlinks=False)


def _tree_nodes(root: Path) -> tuple[Path, ...]:
    nodes = [root]
    for current, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        traversable: list[str] = []
        for name in directory_names:
            child = current_path / name
            if child.is_symlink():
                raise ControlPlaneProtectionError(
                    f"managed control-plane tree contains a symlink: {child}"
                )
            traversable.append(name)
            nodes.append(child)
        directory_names[:] = traversable
        for name in file_names:
            child = current_path / name
            if child.is_symlink():
                raise ControlPlaneProtectionError(
                    f"managed control-plane tree contains a symlink: {child}"
                )
            nodes.append(child)
    return tuple(
        sorted(
            nodes,
            key=lambda path: (len(path.parts), str(path)),
            reverse=True,
        )
    )


def _removable_tree_nodes(root: Path) -> tuple[Path, ...]:
    nodes = [root]
    for current, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        traversable: list[str] = []
        for name in directory_names:
            child = current_path / name
            if child.is_symlink():
                continue
            traversable.append(name)
            nodes.append(child)
        directory_names[:] = traversable
        nodes.extend(
            current_path / name
            for name in file_names
            if not (current_path / name).is_symlink()
        )
    return tuple(
        sorted(
            nodes,
            key=lambda path: (len(path.parts), str(path)),
        )
    )


def _reject_symlink_components(root: Path, relative: PurePosixPath) -> None:
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ControlPlaneProtectionError(
                f"managed control-plane path contains a symlink: {relative}"
            )
        if not current.exists():
            return
