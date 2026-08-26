"""Resolve local inference-engine binaries without hardcoding unverified paths."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

MLX_INSTALL = "uv tool install mlx-lm"
LLAMA_INSTALL = "brew install llama.cpp"


class EngineError(RuntimeError):
    """Raised when a local engine binary cannot be used."""


def _uv_tool_dir() -> Path | None:
    uv = shutil.which("uv")
    if uv is None:
        fallback = Path.home() / ".local/share/uv/tools"
        return fallback if fallback.is_dir() else None
    try:
        completed = subprocess.run(
            [uv, "tool", "dir"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    path = Path(completed.stdout.strip())
    return path if path.is_dir() else None


def mlx_lm_python() -> Path | None:
    tool_dir = _uv_tool_dir()
    if tool_dir is None:
        return None
    python = tool_dir / "mlx-lm" / "bin" / "python"
    return python if python.is_file() else None


def resolve_mlx_server() -> list[str]:
    found = shutil.which("mlx_lm.server")
    if found:
        return [found]
    python = mlx_lm_python()
    if python is not None:
        return [str(python), "-m", "mlx_lm", "server"]
    uv = shutil.which("uv")
    if uv:
        return [uv, "tool", "run", "--from", "mlx-lm", "mlx_lm.server"]
    uvx = shutil.which("uvx")
    if uvx:
        return [uvx, "--from", "mlx-lm", "mlx_lm.server"]
    raise EngineError(f"mlx_lm.server not found. Install with: {MLX_INSTALL}")


def resolve_mlx_python() -> list[str]:
    python = mlx_lm_python()
    if python is not None:
        return [str(python)]
    uv = shutil.which("uv")
    if uv:
        return [uv, "tool", "run", "--from", "mlx-lm", "python"]
    uvx = shutil.which("uvx")
    if uvx:
        return [uvx, "--from", "mlx-lm", "python"]
    raise EngineError(f"mlx-lm Python not found. Install with: {MLX_INSTALL}")


def resolve_llama_server() -> list[str]:
    found = shutil.which("llama-server")
    if found:
        return [found]
    for candidate in (
        Path("/opt/homebrew/bin/llama-server"),
        Path("/usr/local/bin/llama-server"),
    ):
        if os.access(candidate, os.X_OK):
            return [str(candidate)]
    raise EngineError(f"llama-server not found. Install with: {LLAMA_INSTALL}")
