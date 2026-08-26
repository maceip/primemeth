"""Local inference-engine lifecycle: start, stop, status."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import BinaryIO, Literal, cast
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from synth.engine import EngineError, resolve_llama_server, resolve_mlx_server

DEFAULT_PORT = 8080
DEFAULT_HOST = "127.0.0.1"
DEFAULT_MLX_MODEL = Path("/Users/mac/models/Qwen3.8-Flash-Next-MLX-mixed46")
HEALTH_TIMEOUT_SECONDS = 300.0
STOP_TIMEOUT_SECONDS = 8.0
YARN_CONTEXT_PROFILE = "yarn-1m"
EngineName = Literal["mlx", "llama.cpp"]
ContextProfile = Literal["native", "yarn-1m"]


class ServeError(RuntimeError):
    """Raised when the local engine cannot be managed."""


def serve_dir(workspace: Path) -> Path:
    return workspace.resolve() / ".synth" / "serve"


def state_path(workspace: Path) -> Path:
    return serve_dir(workspace) / "state.json"


def log_path(workspace: Path) -> Path:
    return serve_dir(workspace) / "server.log"


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ServeError(f"cannot read {path}: {error}") from error


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_state(workspace: Path) -> dict[str, object] | None:
    path = state_path(workspace)
    if not path.is_file():
        return None
    raw = _load_json(path)
    if not isinstance(raw, dict):
        raise ServeError(f"serve state is not an object: {path}")
    return cast(dict[str, object], raw)


def _write_state(workspace: Path, state: dict[str, object]) -> None:
    directory = serve_dir(workspace)
    directory.mkdir(parents=True, exist_ok=True)
    path = state_path(workspace)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def _clear_state(workspace: Path) -> None:
    path = state_path(workspace)
    if path.exists():
        path.unlink()


def _context_profile(workspace: Path, configured: str | None) -> ContextProfile:
    if configured in ("native", YARN_CONTEXT_PROFILE):
        return cast(ContextProfile, configured)
    path = workspace / "synth.json"
    if not path.is_file():
        return "native"
    raw = _load_json(path)
    if not isinstance(raw, dict):
        return "native"
    qwen = raw.get("qwen")
    if not isinstance(qwen, dict):
        return "native"
    profile = qwen.get("contextProfile")
    if profile == YARN_CONTEXT_PROFILE:
        return "yarn-1m"
    return "native"


def _yarn_params(model: Path) -> tuple[float, int]:
    factor = 4.0
    orig_ctx = 262144
    candidates = []
    if model.is_dir():
        candidates.append(model / "config.json")
    else:
        candidates.append(model.parent / "config.json")
        sibling = model.parent.parent / "Qwen3.8-Flash-Next" / "config.json"
        candidates.append(sibling)
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
            if isinstance(rope.get("factor"), (int, float)):
                factor = float(rope["factor"])
            orig = rope.get("original_max_position_embeddings")
            if isinstance(orig, int):
                orig_ctx = orig
        max_pos = mapping.get("max_position_embeddings")
        if isinstance(max_pos, int) and not (
            isinstance(rope, dict)
            and isinstance(rope.get("original_max_position_embeddings"), int)
        ):
            orig_ctx = max_pos
        return factor, orig_ctx
    return factor, orig_ctx


def _gguf_model(model: Path) -> Path:
    if model.is_file():
        return model
    if not model.is_dir():
        raise ServeError(f"model path does not exist: {model}")
    ggufs = sorted(
        path
        for path in model.rglob("*.gguf")
        if path.is_file() and "mmproj" not in path.name.lower()
    )
    if not ggufs:
        raise ServeError(f"no GGUF weights under {model}")
    return ggufs[0]


def _mlx_argv(model: Path, host: str, port: int) -> list[str]:
    return [
        *resolve_mlx_server(),
        "--model",
        str(model),
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
        factor, orig_ctx = _yarn_params(model)
        argv.extend(
            [
                "--rope-scaling",
                "yarn",
                "--rope-scale",
                str(factor),
                "--yarn-orig-ctx",
                str(orig_ctx),
                "--ctx-size",
                str(int(orig_ctx * factor)),
            ]
        )
    return argv


def _endpoint_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/v1"


def _models_url(host: str, port: int) -> str:
    return f"{_endpoint_url(host, port)}/models"


def health_check(host: str, port: int, timeout: float = 5.0) -> tuple[int, object]:
    opener = build_opener(ProxyHandler({}))
    request = Request(_models_url(host, port), method="GET")
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            status = int(response.status)
    except HTTPError as error:
        raise ServeError(
            f"engine health check returned HTTP {error.code} at "
            f"{_models_url(host, port)}"
        ) from error
    except (OSError, URLError) as error:
        raise ServeError(
            f"engine is not reachable at {_models_url(host, port)}: {error}"
        ) from error
    try:
        decoded: object = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ServeError("engine /v1/models returned invalid JSON") from error
    return status, decoded


def _wait_for_health(
    pid: int, host: str, port: int, log_file: Path, timeout: float
) -> tuple[int, object]:
    deadline = time.monotonic() + timeout
    last_error = "engine did not become ready"
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            detail = ""
            if log_file.is_file():
                detail = log_file.read_text()[-4000:]
            raise ServeError(
                "engine process exited before becoming healthy"
                + (f":\n{detail}" if detail else "")
            )
        try:
            return health_check(host, port)
        except ServeError as error:
            last_error = str(error)
            time.sleep(0.5)
    raise ServeError(last_error)


def _resolve_model(model: Path | None) -> Path:
    if model is not None:
        resolved = model.expanduser().resolve()
        if not resolved.exists():
            raise ServeError(f"model path does not exist: {resolved}")
        return resolved
    if DEFAULT_MLX_MODEL.exists():
        return DEFAULT_MLX_MODEL
    raise ServeError(
        f"pass --model <mlx-or-gguf-path> (default {DEFAULT_MLX_MODEL} is not present)"
    )


def start(
    workspace: Path,
    *,
    engine: EngineName,
    model: Path | None,
    port: int = DEFAULT_PORT,
    host: str = DEFAULT_HOST,
    context_profile: str | None = None,
    alias: str | None = None,
    timeout_seconds: float = HEALTH_TIMEOUT_SECONDS,
) -> dict[str, object]:
    existing = _read_state(workspace)
    if existing is not None:
        pid_raw = existing.get("pid")
        if isinstance(pid_raw, int) and _pid_alive(pid_raw):
            raise ServeError(
                f"engine already running pid={pid_raw} "
                f"endpoint={existing.get('endpoint', _endpoint_url(host, port))}"
            )
        _clear_state(workspace)
    resolved_model = _resolve_model(model)
    profile = _context_profile(workspace, context_profile)
    model_alias = alias or resolved_model.name
    try:
        if engine == "mlx":
            argv = _mlx_argv(resolved_model, host, port)
        elif engine == "llama.cpp":
            argv = _llama_argv(resolved_model, host, port, model_alias, profile)
        else:
            raise ServeError(f"unknown engine: {engine}")
    except EngineError as error:
        raise ServeError(str(error)) from error
    directory = serve_dir(workspace)
    directory.mkdir(parents=True, exist_ok=True)
    log_file = log_path(workspace)
    with log_file.open("wb") as log:
        pid = _spawn(argv, log)
    endpoint = _endpoint_url(host, port)
    state: dict[str, object] = {
        "pid": pid,
        "engine": engine,
        "model": str(resolved_model),
        "port": port,
        "host": host,
        "endpoint": endpoint,
        "argv": argv,
    }
    _write_state(workspace, state)
    try:
        health_status, models = _wait_for_health(
            pid, host, port, log_file, timeout_seconds
        )
    except ServeError as error:
        if not _pid_alive(pid):
            _clear_state(workspace)
        raise ServeError(
            f"engine launched but is not healthy; launched={' '.join(argv)} "
            f"endpoint={endpoint}; {error}"
        ) from error
    state["health"] = health_status
    state["models"] = models
    _write_state(workspace, state)
    return state


def _spawn(argv: list[str], log: BinaryIO) -> int:
    try:
        process = subprocess.Popen(
            argv,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except FileNotFoundError as error:
        raise ServeError(f"failed to launch {argv[0]}: {error}") from error
    if process.pid is None:
        raise ServeError("engine launched without a pid")
    return process.pid


def _terminate(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return


def stop(workspace: Path) -> dict[str, object]:
    state = _read_state(workspace)
    if state is None:
        raise ServeError("no local engine is running")
    pid_raw = state.get("pid")
    if not isinstance(pid_raw, int):
        _clear_state(workspace)
        raise ServeError("serve state is missing pid")
    if _pid_alive(pid_raw):
        _terminate(pid_raw)
    _clear_state(workspace)
    return {"stopped": True, "pid": pid_raw}


def status(workspace: Path) -> dict[str, object]:
    state = _read_state(workspace)
    if state is None:
        raise ServeError("no local engine is running")
    pid_raw = state.get("pid")
    port_raw = state.get("port")
    host_raw = state.get("host")
    host = host_raw if isinstance(host_raw, str) and host_raw else DEFAULT_HOST
    if not isinstance(pid_raw, int) or not isinstance(port_raw, int):
        raise ServeError("serve state is missing pid or port")
    alive = _pid_alive(pid_raw)
    result: dict[str, object] = {
        "pid": pid_raw,
        "engine": state.get("engine"),
        "model": state.get("model"),
        "port": port_raw,
        "alive": alive,
    }
    if not alive:
        raise ServeError(
            f"engine pid {pid_raw} is not running "
            f"(stale state at {state_path(workspace)})"
        )
    http_status, models = health_check(host, port_raw)
    result["health"] = http_status
    result["endpoint"] = _models_url(host, port_raw)
    result["models"] = models
    return result
