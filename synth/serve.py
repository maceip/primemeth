"""MLX and llama.cpp adapters for the shared serving lifecycle."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Literal, cast

from synth.config import load_synth_settings
from synth.engine import (
    EngineError,
    require_mlx_model_support,
    resolve_llama_server,
    resolve_mlx_server,
)
from synth.serving import ManagedServeRequest, start_managed_server

DEFAULT_PORT = 8080
DEFAULT_HOST = "127.0.0.1"
HEALTH_TIMEOUT_SECONDS = 300.0
YARN_CONTEXT_PROFILE = "yarn-1m"
LocalEngineName = Literal["mlx", "llama.cpp"]
ContextProfile = Literal["native", "yarn-1m"]
LOCAL_ENGINE_NAMES: tuple[LocalEngineName, ...] = ("mlx", "llama.cpp")
_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ServeError(RuntimeError):
    """Raised when a local engine launch cannot be prepared."""


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ServeError(f"cannot read {path}: {error}") from error


def _context_profile(workspace: Path, configured: str | None) -> ContextProfile:
    if configured in ("native", YARN_CONTEXT_PROFILE):
        return cast(ContextProfile, configured)
    settings = load_synth_settings(workspace)
    if settings.qwen is not None:
        return settings.qwen.context_profile
    return "native"


def _offline_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "NO_PROXY": "localhost,127.0.0.1,::1",
            "no_proxy": "localhost,127.0.0.1,::1",
            "PYTHONUNBUFFERED": "1",
            "TOKENIZERS_PARALLELISM": "false",
        }
    )
    return environment


def _yarn_params(model: Path) -> tuple[float, int]:
    factor = 4.0
    original_context = 262_144
    candidates = [model / "config.json"] if model.is_dir() else []
    candidates.extend(
        (
            model.parent / "config.json",
            model.parent.parent / "Qwen3.8-Flash-Next" / "config.json",
        )
    )
    for config_path in candidates:
        if not config_path.is_file():
            continue
        raw = _load_json(config_path)
        if not isinstance(raw, dict):
            continue
        text = raw.get("text_config")
        mapping = text if isinstance(text, dict) else raw
        rope = mapping.get("rope_parameters") or mapping.get("rope_scaling")
        if isinstance(rope, dict):
            configured_factor = rope.get("factor")
            if isinstance(configured_factor, (int, float)):
                factor = float(configured_factor)
            configured_original = rope.get("original_max_position_embeddings")
            if isinstance(configured_original, int):
                original_context = configured_original
        max_position = mapping.get("max_position_embeddings")
        if isinstance(max_position, int) and not (
            isinstance(rope, dict)
            and isinstance(rope.get("original_max_position_embeddings"), int)
        ):
            original_context = max_position
        return factor, original_context
    return factor, original_context


def _gguf_model(model: Path) -> Path:
    if model.is_file():
        if model.suffix.casefold() != ".gguf":
            raise ServeError(f"llama.cpp model is not a GGUF file: {model}")
        return model
    candidates = sorted(
        path
        for path in model.rglob("*.gguf")
        if path.is_file() and "mmproj" not in path.name.casefold()
    )
    if not candidates:
        raise ServeError(f"no GGUF weights under {model}")
    if len(candidates) > 1:
        raise ServeError(
            f"multiple GGUF weights found under {model}; pass an exact file path"
        )
    return candidates[0]


def _mlx_argv(model: str, host: str, port: int) -> list[str]:
    return [
        *resolve_mlx_server(),
        "--model",
        model,
        "--host",
        host,
        "--port",
        str(port),
    ]


def _llama_argv(
    model: Path,
    host: str,
    port: int,
    alias: str,
    profile: ContextProfile,
) -> list[str]:
    argv = [
        *resolve_llama_server(),
        "--model",
        str(_gguf_model(model)),
        "--host",
        host,
        "--port",
        str(port),
        "--jinja",
        "--alias",
        alias,
    ]
    if profile == YARN_CONTEXT_PROFILE:
        factor, original_context = _yarn_params(model)
        argv.extend(
            [
                "--rope-scaling",
                "yarn",
                "--rope-scale",
                str(factor),
                "--yarn-orig-ctx",
                str(original_context),
                "--ctx-size",
                str(int(original_context * factor)),
            ]
        )
    return argv


def _resolve_model(model: Path | None) -> Path:
    if model is None:
        raise ServeError("--model is required for mlx and llama.cpp")
    try:
        return model.expanduser().resolve(strict=True)
    except OSError as error:
        raise ServeError(f"model path is unavailable: {model}: {error}") from error


def _default_alias(model: Path) -> str:
    if "qwen3.8-flash-next" in model.name.casefold():
        return "qwen3.8-flash-next"
    alias = re.sub(r"[^A-Za-z0-9._-]+", "-", model.name).strip("-.").lower()
    if not alias:
        raise ServeError(f"cannot derive a served model name from {model}")
    return alias


def _validate_alias(alias: str) -> None:
    if _ALIAS_PATTERN.fullmatch(alias) is None:
        raise ServeError(
            "--served-model-name must contain only letters, digits, dot, dash, "
            "or underscore and be at most 128 characters"
        )


def _prepare_mlx_alias(workspace: Path, model: Path, alias: str) -> tuple[str, Path]:
    alias_root = workspace.resolve() / ".synth" / "serve" / "mlx-models"
    alias_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(alias_root, 0o700)
    destination = alias_root / alias
    if destination.exists() and not destination.is_symlink():
        raise ServeError(f"MLX model alias path is not a symlink: {destination}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{alias}.", suffix=".tmp", dir=alias_root
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink()
    try:
        temporary.symlink_to(model, target_is_directory=model.is_dir())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return alias, alias_root


def start(
    workspace: Path,
    *,
    engine: LocalEngineName,
    model: Path | None,
    port: int = DEFAULT_PORT,
    host: str = DEFAULT_HOST,
    context_profile: str | None = None,
    alias: str | None = None,
    timeout_seconds: float = HEALTH_TIMEOUT_SECONDS,
) -> dict[str, object]:
    resolved_model = _resolve_model(model)
    model_alias = alias or _default_alias(resolved_model)
    _validate_alias(model_alias)
    details: dict[str, object] = {"servedModel": model_alias}
    try:
        if engine == "mlx":
            if context_profile not in (None, "native"):
                raise ServeError("mlx does not implement the yarn-1m context profile")
            configured = load_synth_settings(workspace).qwen
            if configured is not None and configured.context_profile != "native":
                raise ServeError(
                    "mlx does not implement synth.json qwen.contextProfile=yarn-1m; "
                    "use native or select llama.cpp"
                )
            model_type = require_mlx_model_support(resolved_model)
            model_argument, working_directory = _prepare_mlx_alias(
                workspace, resolved_model, model_alias
            )
            command = _mlx_argv(model_argument, host, port)
            details["modelType"] = model_type
        elif engine == "llama.cpp":
            profile = _context_profile(workspace, context_profile)
            working_directory = workspace.resolve()
            command = _llama_argv(
                resolved_model,
                host,
                port,
                model_alias,
                profile,
            )
            details["contextProfile"] = profile
        else:
            raise ServeError(f"unknown engine: {engine}")
    except EngineError as error:
        raise ServeError(str(error)) from error
    return start_managed_server(
        workspace,
        ManagedServeRequest(
            engine=engine,
            model=str(resolved_model),
            command=tuple(command),
            host=host,
            port=port,
            startup_timeout_seconds=timeout_seconds,
            working_directory=working_directory,
            environment=_offline_environment(),
            health_probe_model=model_alias,
            details=details,
        ),
    )
