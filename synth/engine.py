"""Resolve local inference-engine binaries without hardcoding unverified paths."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

MLX_INSTALL = "uv tool install mlx-lm"
LLAMA_INSTALL = "brew install llama.cpp"
LLAMA_SERVER_ENV = "SYNTH_LLAMA_SERVER"
_MLX_SUPPORT_PROBE = (
    "import importlib\n"
    "import json\n"
    "import sys\n"
    "from mlx_lm.utils import MODEL_REMAPPING\n"
    "with open(sys.argv[1], encoding='utf-8') as stream:\n"
    "    config = json.load(stream)\n"
    "model_type = config['model_type']\n"
    "module_name = MODEL_REMAPPING.get(model_type, model_type)\n"
    "importlib.import_module(f'mlx_lm.models.{module_name}')\n"
)


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
    raise EngineError(f"mlx_lm.server not found. Install with: {MLX_INSTALL}")


def resolve_mlx_python() -> list[str]:
    python = mlx_lm_python()
    if python is not None:
        return [str(python)]
    raise EngineError(f"mlx-lm Python not found. Install with: {MLX_INSTALL}")


def mlx_model_type(model_dir: Path) -> str:
    config_path = model_dir / "config.json"
    try:
        value: object = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise EngineError(
            f"cannot read MLX model config {config_path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise EngineError(f"MLX model config must be an object: {config_path}")
    model_type = value.get("model_type")
    if not isinstance(model_type, str) or not model_type:
        raise EngineError(f"MLX model config is missing model_type: {config_path}")
    return model_type


def require_mlx_model_support(model_dir: Path) -> str:
    model_type = mlx_model_type(model_dir)
    python = resolve_mlx_python()[0]
    environment = os.environ.copy()
    environment.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "NO_PROXY": "localhost,127.0.0.1,::1",
            "no_proxy": "localhost,127.0.0.1,::1",
        }
    )
    try:
        completed = subprocess.run(
            [python, "-c", _MLX_SUPPORT_PROBE, str(model_dir / "config.json")],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
    except OSError as error:
        raise EngineError(f"cannot probe MLX model support: {error}") from error
    if completed.returncode != 0:
        lines = (completed.stderr or completed.stdout).strip().splitlines()
        detail = lines[-1] if lines else f"probe exited {completed.returncode}"
        raise EngineError(
            f"installed mlx-lm does not support model_type {model_type!r}: "
            f"{detail}. Install a locally validated mlx-lm build that supports "
            f"{model_type!r} before serving or quantizing this model."
        )
    return model_type


def resolve_llama_server() -> list[str]:
    configured = os.environ.get(LLAMA_SERVER_ENV)
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            raise EngineError(f"{LLAMA_SERVER_ENV} must be an absolute path")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise EngineError(
                f"{LLAMA_SERVER_ENV} is unavailable: {candidate}: {error}"
            ) from error
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise EngineError(
                f"{LLAMA_SERVER_ENV} is not an executable file: {resolved}"
            )
        return [str(resolved)]
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
