"""Dedicated local model-engine setup and serving lifecycle."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

EngineName = Literal["transformers"]
Quantization = Literal["bnb-4bit", "bnb-8bit"]
DeviceName = Literal["mps", "auto", "cpu"]

ENGINE_NAMES: tuple[EngineName, ...] = ("transformers",)
QUANTIZATIONS: tuple[Quantization, ...] = ("bnb-4bit", "bnb-8bit")
DEVICE_NAMES: tuple[DeviceName, ...] = ("mps", "auto", "cpu")

TRANSFORMERS_REQUIREMENTS = (
    "transformers[serving]>=5.16",
    "bitsandbytes>=0.50.1",
    "kernels",
    "torch>=2.9",
    "accelerate",
)
TRANSFORMERS_DISTRIBUTIONS = (
    "transformers",
    "bitsandbytes",
    "kernels",
    "torch",
    "accelerate",
)
TRANSFORMERS_SERVE_FLAGS = (
    "--quantization",
    "--device",
    "--attn-implementation",
    "--host",
    "--port",
    "--no-continuous-batching",
    "--no-compile",
    "--log-level",
)
METAL_KERNEL_REPOSITORY = "kernels-community/metal-flash-sdpa"
METAL_KERNEL_VERSION = 1
BITSANDBYTES_KERNEL_REPOSITORY = "kernels-community/bitsandbytes-mps"
BITSANDBYTES_KERNEL_VERSION = 1
MPS_KERNEL_SPECS = (
    (METAL_KERNEL_REPOSITORY, METAL_KERNEL_VERSION),
    (BITSANDBYTES_KERNEL_REPOSITORY, BITSANDBYTES_KERNEL_VERSION),
)
QWEN_TRANSFORMERS_MODEL_ID = "qwen3.8-flash-next-transformers"
DEFAULT_PORT = 8000
DEFAULT_STARTUP_TIMEOUT_SECONDS = 900.0
STATE_SCHEMA_VERSION = 2
_LOCAL_BIND_HOST = "127.0.0.1"
_PUBLIC_HOST = "localhost"
_LARGE_MODEL_BYTES = 80_000_000_000
_MAX_SAFETENSORS_HEADER_BYTES = 100_000_000
_WIRED_LIMIT_CAP_MB = 120_000
_WIRED_RESERVE_MB = 8_192
_MODEL_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHARD_PATTERN = re.compile(
    r"^(?P<prefix>.+)-(?P<part>\d{5})-of-(?P<total>\d{5})\.safetensors$"
)


class ServingError(RuntimeError):
    """Raised when a local serving engine cannot be managed safely."""


@dataclass(frozen=True, slots=True)
class ServeRequest:
    engine: EngineName
    model: str
    quantization: Quantization | None = None
    device: DeviceName = "mps"
    port: int = DEFAULT_PORT
    startup_timeout_seconds: float = DEFAULT_STARTUP_TIMEOUT_SECONDS
    served_model_name: str | None = None


@dataclass(frozen=True, slots=True)
class _ProcessIdentity:
    process_group_id: int
    birth_token: str
    command: str


@dataclass(frozen=True, slots=True)
class _ModelInventory:
    model_argument: str
    local_path: Path | None
    model_type: str | None
    index_path: Path | None
    weight_bytes: int | None
    estimated_weight_bytes: int | None
    present_shards: int | None
    expected_shards: int | None
    missing_shards: tuple[str, ...]
    errors: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "modelArgument": self.model_argument,
            "localPath": str(self.local_path) if self.local_path is not None else None,
            "modelType": self.model_type,
            "indexPath": str(self.index_path) if self.index_path is not None else None,
            "weightBytes": self.weight_bytes,
            "estimatedWeightBytes": self.estimated_weight_bytes,
            "presentShards": self.present_shards,
            "expectedShards": self.expected_shards,
            "missingShards": list(self.missing_shards),
        }


@dataclass(frozen=True, slots=True)
class _Preflight:
    inventory: _ModelInventory
    physical_memory_bytes: int | None
    free_memory_percent: int | None
    available_memory_bytes: int | None
    wired_limit_mb: int | None
    wired_limit_hint: str | None
    warnings: tuple[str, ...]
    errors: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "model": self.inventory.as_dict(),
            "physicalMemoryBytes": self.physical_memory_bytes,
            "freeMemoryPercent": self.free_memory_percent,
            "availableMemoryBytes": self.available_memory_bytes,
            "wiredLimitMb": self.wired_limit_mb,
            "wiredLimitHint": self.wired_limit_hint,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


@dataclass(frozen=True, slots=True)
class _ServeState:
    schema_version: int
    engine: str
    status: str
    pid: int
    process_group_id: int
    process_birth_token: str
    process_command: str
    started_at: str
    updated_at: str
    stopped_at: str | None
    host: str
    port: int
    endpoint: str
    command: tuple[str, ...]
    log: str
    health_models: tuple[str, ...]
    details: dict[str, object]
    error: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "engine": self.engine,
            "status": self.status,
            "pid": self.pid,
            "processGroupId": self.process_group_id,
            "processBirthToken": self.process_birth_token,
            "processCommand": self.process_command,
            "startedAt": self.started_at,
            "updatedAt": self.updated_at,
            "stoppedAt": self.stopped_at,
            "host": self.host,
            "port": self.port,
            "endpoint": self.endpoint,
            "command": list(self.command),
            "log": self.log,
            "healthModels": list(self.health_models),
            "details": self.details,
            "error": self.error,
        }

    @classmethod
    def from_object(cls, value: object, path: Path) -> _ServeState:
        mapping = _mapping(value, f"serve state {path}")
        schema_version = _integer(mapping.get("schemaVersion"), "schemaVersion")
        if schema_version != STATE_SCHEMA_VERSION:
            raise ServingError(
                f"unsupported serve state schema {schema_version} in {path}"
            )
        command_value = mapping.get("command")
        if not isinstance(command_value, list) or not all(
            isinstance(item, str) for item in command_value
        ):
            raise ServingError(f"serve state command must be a string array: {path}")
        health_value = mapping.get("healthModels", [])
        if not isinstance(health_value, list) or not all(
            isinstance(item, str) for item in health_value
        ):
            raise ServingError(
                f"serve state healthModels must be a string array: {path}"
            )
        return cls(
            schema_version=schema_version,
            engine=_string(mapping.get("engine"), "engine"),
            status=_string(mapping.get("status"), "status"),
            pid=_integer(mapping.get("pid"), "pid"),
            process_group_id=_integer(mapping.get("processGroupId"), "processGroupId"),
            process_birth_token=_string(
                mapping.get("processBirthToken"), "processBirthToken"
            ),
            process_command=_string(mapping.get("processCommand"), "processCommand"),
            started_at=_string(mapping.get("startedAt"), "startedAt"),
            updated_at=_string(mapping.get("updatedAt"), "updatedAt"),
            stopped_at=_optional_string(mapping.get("stoppedAt"), "stoppedAt"),
            host=_string(mapping.get("host"), "host"),
            port=_integer(mapping.get("port"), "port"),
            endpoint=_string(mapping.get("endpoint"), "endpoint"),
            command=tuple(cast(list[str], command_value)),
            log=_string(mapping.get("log"), "log"),
            health_models=tuple(cast(list[str], health_value)),
            details=_mapping(mapping.get("details"), "details"),
            error=_optional_string(mapping.get("error"), "error"),
        )


def setup_engine(engine: EngineName) -> dict[str, object]:
    if engine != "transformers":
        raise ServingError(f"unsupported serving engine: {engine}")
    uv = shutil.which("uv")
    if uv is None:
        raise ServingError("uv is required to set up the transformers engine")
    venv = _transformers_venv()
    python = venv / "bin" / "python"
    if venv.exists():
        if not venv.is_dir() or not (venv / "pyvenv.cfg").is_file():
            raise ServingError(f"serving venv path is not a virtualenv: {venv}")
    else:
        venv.parent.mkdir(parents=True, exist_ok=True)
        _run_visible(
            [
                uv,
                "venv",
                "--no-project",
                "--no-config",
                "--managed-python",
                "--python",
                "3.12",
                str(venv),
            ],
            "create transformers serving venv",
        )
    if not python.is_file():
        raise ServingError(f"serving venv has no Python executable: {python}")
    version = _python_version(python)
    if not version.startswith("3.12."):
        raise ServingError(
            f"transformers serving venv must use Python 3.12, found {version}"
        )
    _run_visible(
        [
            uv,
            "pip",
            "install",
            "--no-config",
            "--strict",
            "--upgrade",
            "--python",
            str(python),
            *TRANSFORMERS_REQUIREMENTS,
        ],
        "install transformers serving dependencies",
    )
    packages, mps_available = _installed_engine_metadata(python)
    mps_kernels = (
        tuple(
            _install_mps_kernel(python, repository, version)
            for repository, version in MPS_KERNEL_SPECS
        )
        if mps_available
        else ()
    )
    verified_flags = _verify_transformers_serve_cli(venv)
    receipt_path = _write_engine_receipt(venv, mps_kernels)
    return {
        "engine": engine,
        "venv": str(venv),
        "pythonVersion": version,
        "packages": packages,
        "mpsAvailable": mps_available,
        "mpsKernels": list(mps_kernels),
        "receipt": str(receipt_path),
        "serveCli": {
            "executable": str(_transformers_executable(venv)),
            "forceModelArgument": True,
            "verifiedFlags": list(verified_flags),
        },
    }


def start_server(workspace: Path, request: ServeRequest) -> dict[str, object]:
    if request.engine != "transformers":
        raise ServingError(f"unsupported serving engine: {request.engine}")
    _validate_request(request)
    root = _serve_root(workspace)
    previous = _read_state(_state_path(root))
    if previous is not None and _process_matches(previous):
        raise ServingError(
            f"{previous.engine} server is already running with pid {previous.pid}"
        )
    venv = _transformers_venv()
    executable = _transformers_executable(venv)
    if not executable.is_file():
        raise ServingError(
            "transformers engine is not installed; run "
            "`synth engine setup transformers`"
        )
    _verify_transformers_serve_cli(venv)
    mps_kernels = _cached_mps_kernels(venv)
    preflight = _preflight(request.model, request.quantization)
    if preflight.errors:
        rendered = json.dumps(preflight.as_dict(), sort_keys=True)
        raise ServingError(f"model preflight failed: {rendered}")
    _assert_port_available(request.port)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    model_argument, process_directory = _prepare_model_argument(
        root,
        preflight.inventory,
        request.served_model_name,
    )
    log_path = _log_path(root, request.engine)
    state_path = _state_path(root)
    command = _transformers_command(executable, model_argument, request)
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
    local_kernel_overrides = [
        f"{kernel['repository']}={kernel['repositoryPath']}" for kernel in mps_kernels
    ]
    configured_kernel_overrides = environment.get("LOCAL_KERNELS")
    if configured_kernel_overrides:
        local_kernel_overrides.insert(0, configured_kernel_overrides)
    environment["LOCAL_KERNELS"] = ":".join(local_kernel_overrides)
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.chmod(log_path, 0o600)
    started_at = _utc_now()
    process: subprocess.Popen[str] | None = None
    try:
        with os.fdopen(descriptor, "a", encoding="utf-8") as log_stream:
            log_stream.write(
                f"\n[{started_at}] synth serve start: " + " ".join(command) + "\n"
            )
            log_stream.flush()
            process = subprocess.Popen(
                command,
                cwd=process_directory,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        process_group_id = os.getpgid(process.pid)
        if process_group_id != process.pid:
            raise ServingError(
                "transformers process did not establish a dedicated process group"
            )
        identity = _process_identity(process.pid)
        expected_command = " ".join(command)
        if identity is None or not identity.command.endswith(expected_command):
            raise ServingError(
                "could not verify the complete transformers server process identity"
            )
        if identity.process_group_id != process_group_id:
            raise ServingError(
                "transformers process group changed before state was recorded"
            )
        state = _ServeState(
            schema_version=STATE_SCHEMA_VERSION,
            engine=request.engine,
            status="starting",
            pid=process.pid,
            process_group_id=process_group_id,
            process_birth_token=identity.birth_token,
            process_command=identity.command,
            started_at=started_at,
            updated_at=started_at,
            stopped_at=None,
            host=_LOCAL_BIND_HOST,
            port=request.port,
            endpoint=_local_http_url(_PUBLIC_HOST, request.port, "/v1"),
            command=tuple(command),
            log=str(log_path),
            health_models=(),
            details={
                "model": (
                    str(preflight.inventory.local_path)
                    if preflight.inventory.local_path is not None
                    else request.model
                ),
                "servedModel": model_argument,
                "quantization": request.quantization,
                "device": request.device,
                "venv": str(venv),
                "preflight": preflight.as_dict(),
            },
            error=None,
        )
        _write_state(state_path, state)
        health_models = _wait_until_healthy(
            process,
            _LOCAL_BIND_HOST,
            request.port,
            request.startup_timeout_seconds,
        )
        running = replace(
            state,
            status="running",
            updated_at=_utc_now(),
            health_models=health_models,
        )
        _write_state(state_path, running)
        return running.as_dict()
    except BaseException as error:
        with suppress(OSError):
            os.close(descriptor)
        if process is not None:
            _terminate_spawned_process(process)
            failed_at = _utc_now()
            existing = _read_state(state_path)
            if existing is not None and existing.pid == process.pid:
                detail = str(error)
                log_tail = _tail_log(log_path)
                if log_tail:
                    detail = f"{detail}; log tail: {log_tail}"
                _write_state(
                    state_path,
                    replace(
                        existing,
                        status="failed",
                        updated_at=failed_at,
                        stopped_at=failed_at,
                        error=detail,
                    ),
                )
        if not isinstance(error, Exception):
            raise
        if isinstance(error, ServingError):
            raise
        raise ServingError(f"could not start transformers server: {error}") from error


def serve_status(workspace: Path, engine: str | None = None) -> dict[str, object]:
    root = _serve_root(workspace)
    state = _read_state(_state_path(root))
    if state is None or (engine is not None and state.engine != engine):
        if engine is not None:
            return _not_configured(root, engine)
        return {"stateDir": str(root), "engines": []}
    current = _refresh_state(root, state)
    if engine is not None:
        return current.as_dict()
    return {"stateDir": str(root), "engines": [current.as_dict()]}


def stop_server(workspace: Path, engine: str | None = None) -> dict[str, object]:
    root = _serve_root(workspace)
    state_path = _state_path(root)
    state = _read_state(state_path)
    if state is None:
        if engine is not None:
            return _not_configured(root, engine)
        return serve_status(workspace)
    if engine is not None and state.engine != engine:
        return _not_configured(root, engine)
    if not _process_matches(state):
        stale = _mark_stale_if_needed(state_path, state)
        return stale.as_dict()
    with suppress(ProcessLookupError):
        os.killpg(state.process_group_id, signal.SIGTERM)
    if not _wait_for_exit(state.pid, 10.0):
        with suppress(ProcessLookupError):
            os.killpg(state.process_group_id, signal.SIGKILL)
        if not _wait_for_exit(state.pid, 5.0):
            raise ServingError(
                f"server process group {state.process_group_id} did not stop"
            )
    stopped_at = _utc_now()
    stopped = replace(
        state,
        status="stopped",
        updated_at=stopped_at,
        stopped_at=stopped_at,
        error=None,
    )
    _write_state(state_path, stopped)
    return stopped.as_dict()


def _transformers_venv() -> Path:
    return Path.home() / ".venvs" / "pm-transformers"


def _transformers_executable(venv: Path) -> Path:
    return venv / "bin" / "transformers"


def _run_visible(command: list[str], action: str) -> None:
    try:
        result = subprocess.run(command, check=False)
    except OSError as error:
        raise ServingError(f"could not {action}: {error}") from error
    if result.returncode != 0:
        raise ServingError(f"could not {action} (exit {result.returncode})")


def _run_captured(
    command: list[str], action: str, *, environment: dict[str, str] | None = None
) -> str:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
    except OSError as error:
        raise ServingError(f"could not {action}: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        suffix = f": {detail}" if detail else ""
        raise ServingError(f"could not {action} (exit {result.returncode}){suffix}")
    return result.stdout


def _python_version(python: Path) -> str:
    return _run_captured(
        [str(python), "-c", "import platform; print(platform.python_version())"],
        "read serving Python version",
    ).strip()


def _installed_engine_metadata(python: Path) -> tuple[dict[str, str], bool]:
    names = json.dumps(TRANSFORMERS_DISTRIBUTIONS)
    script = (
        "import importlib.metadata as metadata\n"
        "import json\n"
        "import torch\n"
        f"names = {names}\n"
        "print(json.dumps({'packages': {name: metadata.version(name) "
        "for name in names}, 'mpsAvailable': torch.backends.mps.is_available()}))\n"
    )
    output = _run_captured(
        [str(python), "-c", script],
        "inspect transformers serving environment",
    )
    try:
        decoded: object = json.loads(output)
    except json.JSONDecodeError as error:
        raise ServingError("serving environment returned invalid metadata") from error
    mapping = _mapping(decoded, "serving environment metadata")
    packages_value = _mapping(mapping.get("packages"), "installed packages")
    if not all(
        isinstance(name, str) and isinstance(version, str)
        for name, version in packages_value.items()
    ):
        raise ServingError("serving environment package versions are invalid")
    mps_available = mapping.get("mpsAvailable")
    if not isinstance(mps_available, bool):
        raise ServingError("serving environment MPS status is invalid")
    return cast(dict[str, str], packages_value), mps_available


def _install_mps_kernel(python: Path, repository: str, version: int) -> dict[str, str]:
    script = (
        "import json\n"
        "from kernels import install_kernel\n"
        "from kernels._versions import resolve_version_spec_as_ref\n"
        f"ref = resolve_version_spec_as_ref({repository!r}, {version})\n"
        f"path = install_kernel({repository!r}, "
        "revision=ref.target_commit)\n"
        "print(json.dumps({'revision': ref.target_commit, 'path': str(path)}))\n"
    )
    output = _run_captured(
        [str(python), "-c", script],
        f"prepare MPS kernel {repository}",
    )
    return _kernel_metadata(output, repository, version)


def _cached_mps_kernels(venv: Path) -> tuple[dict[str, str], ...]:
    receipt_path = _engine_receipt_path(venv)
    try:
        receipt_value: object = json.loads(receipt_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ServingError(
            "transformers engine setup receipt is unavailable; run "
            "`synth engine setup transformers`"
        ) from error
    receipt = _mapping(receipt_value, "transformers engine setup receipt")
    if _integer(receipt.get("schemaVersion"), "schemaVersion") != 1:
        raise ServingError("unsupported transformers engine setup receipt")
    kernels_value = receipt.get("kernels")
    if not isinstance(kernels_value, list):
        raise ServingError(
            "MPS kernel setup receipt is invalid; run `synth engine setup transformers`"
        )
    kernels: list[dict[str, str]] = []
    for value in kernels_value:
        kernel = _mapping(value, "MPS kernel setup receipt")
        repository = _string(kernel.get("repository"), "repository")
        version = _string(kernel.get("version"), "version")
        revision = _string(kernel.get("revision"), "revision")
        variant_path = Path(_string(kernel.get("path"), "path")).resolve()
        repository_path = Path(
            _string(kernel.get("repositoryPath"), "repositoryPath")
        ).resolve()
        if (
            not variant_path.is_dir()
            or not (variant_path / "metadata.json").is_file()
            or not repository_path.is_dir()
        ):
            raise ServingError(
                "cached MPS kernel is incomplete; run `synth engine setup transformers`"
            )
        try:
            variant_path.relative_to(repository_path / "build")
        except ValueError as error:
            raise ServingError("cached MPS kernel receipt has invalid paths") from error
        kernels.append(
            {
                "repository": repository,
                "version": version,
                "revision": revision,
                "path": str(variant_path),
                "repositoryPath": str(repository_path),
            }
        )
    expected = {(repository, str(version)) for repository, version in MPS_KERNEL_SPECS}
    actual = {(kernel["repository"], kernel["version"]) for kernel in kernels}
    if actual != expected:
        raise ServingError(
            "MPS kernel setup receipt does not match the serving engine; run "
            "`synth engine setup transformers`"
        )
    return tuple(kernels)


def _engine_receipt_path(venv: Path) -> Path:
    return venv / "synth-engine.json"


def _write_engine_receipt(venv: Path, kernels: tuple[dict[str, str], ...]) -> Path:
    if not kernels:
        raise ServingError("MPS is unavailable; cannot prepare the MPS serving engine")
    path = _engine_receipt_path(venv)
    payload = (
        json.dumps(
            {
                "schemaVersion": 1,
                "engine": "transformers",
                "kernels": list(kernels),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".synth-engine.", suffix=".tmp", dir=venv
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    return path


def _kernel_metadata(output: str, repository: str, version: int) -> dict[str, str]:
    try:
        decoded: object = json.loads(output)
    except json.JSONDecodeError as error:
        raise ServingError("MPS kernel setup returned invalid metadata") from error
    mapping = _mapping(decoded, "MPS kernel metadata")
    revision = _string(mapping.get("revision"), "revision")
    path = Path(_string(mapping.get("path"), "path")).resolve()
    repository_path = path.parent.parent
    return {
        "repository": repository,
        "version": str(version),
        "revision": revision,
        "path": str(path),
        "repositoryPath": str(repository_path),
    }


def _verify_transformers_serve_cli(venv: Path) -> tuple[str, ...]:
    executable = _transformers_executable(venv)
    help_text = _run_captured(
        [str(executable), "serve", "--help"],
        "inspect transformers serve CLI",
    )
    missing = [flag for flag in TRANSFORMERS_SERVE_FLAGS if flag not in help_text]
    if "[FORCE_MODEL]" not in help_text:
        missing.append("FORCE_MODEL")
    if missing:
        raise ServingError(
            "installed transformers serve CLI lacks required arguments: "
            + ", ".join(missing)
        )
    return TRANSFORMERS_SERVE_FLAGS


def _validate_request(request: ServeRequest) -> None:
    if not request.model.strip():
        raise ServingError("--model must be nonempty")
    if not 1 <= request.port <= 65_535:
        raise ServingError("--port must be from 1 to 65535")
    if not 1.0 <= request.startup_timeout_seconds <= 7_200.0:
        raise ServingError("--startup-timeout-seconds must be from 1 to 7200")
    if request.served_model_name is not None:
        _validate_model_alias(request.served_model_name)


def _preflight(model: str, quantization: Quantization | None) -> _Preflight:
    inventory = _inspect_model(model)
    physical_memory_bytes = _sysctl_integer("hw.memsize")
    wired_limit_mb = _sysctl_integer("iogpu.wired_limit_mb")
    free_memory_percent = _free_memory_percent()
    available_memory_bytes = (
        physical_memory_bytes * free_memory_percent // 100
        if physical_memory_bytes is not None and free_memory_percent is not None
        else None
    )
    errors = list(inventory.errors)
    warnings: list[str] = []
    if inventory.local_path is None:
        warnings.append(
            "remote model IDs run with HF_HUB_OFFLINE=1 and must already be cached"
        )
    if inventory.model_type == "qwen4_exp":
        warnings.append(
            "qwen4_exp includes a memory-heavy 51B n-gram table; preserve "
            "additional memory headroom beyond quantized weights"
        )
    estimated = inventory.estimated_weight_bytes or inventory.weight_bytes or 0
    is_large = estimated >= _LARGE_MODEL_BYTES
    wired_limit_hint: str | None = None
    if is_large:
        if physical_memory_bytes is None:
            errors.append("cannot read physical memory for large-model preflight")
        else:
            physical_mb = physical_memory_bytes // (1024 * 1024)
            recommended_mb = min(
                _WIRED_LIMIT_CAP_MB,
                max(0, physical_mb - _WIRED_RESERVE_MB),
            )
            wired_limit_hint = f"sudo sysctl -w iogpu.wired_limit_mb={recommended_mb}"
            if wired_limit_mb is None:
                errors.append("cannot read iogpu.wired_limit_mb")
            elif wired_limit_mb < recommended_mb:
                errors.append(
                    "iogpu.wired_limit_mb is "
                    f"{wired_limit_mb}; raise it before serving this large model "
                    f"(hint only, not executed): {wired_limit_hint}"
                )
        minimum_available = 100_000_000_000 if quantization == "bnb-4bit" else estimated
        if available_memory_bytes is None:
            errors.append("cannot establish available memory for large-model preflight")
        elif available_memory_bytes < minimum_available:
            errors.append(
                "estimated free memory is below the large-model preflight minimum: "
                f"{available_memory_bytes} < {minimum_available} bytes"
            )
    return _Preflight(
        inventory=inventory,
        physical_memory_bytes=physical_memory_bytes,
        free_memory_percent=free_memory_percent,
        available_memory_bytes=available_memory_bytes,
        wired_limit_mb=wired_limit_mb,
        wired_limit_hint=wired_limit_hint,
        warnings=tuple(warnings),
        errors=tuple(errors),
    )


def _inspect_model(model: str) -> _ModelInventory:
    stripped = model.strip()
    candidate = Path(stripped).expanduser()
    path_like = candidate.is_absolute() or stripped.startswith((".", "~"))
    if not path_like and not candidate.exists():
        return _ModelInventory(
            model_argument=stripped,
            local_path=None,
            model_type=None,
            index_path=None,
            weight_bytes=None,
            estimated_weight_bytes=None,
            present_shards=None,
            expected_shards=None,
            missing_shards=(),
            errors=(),
        )
    try:
        local_path = candidate.resolve(strict=True)
    except OSError as error:
        return _ModelInventory(
            model_argument=stripped,
            local_path=None,
            model_type=None,
            index_path=None,
            weight_bytes=None,
            estimated_weight_bytes=None,
            present_shards=None,
            expected_shards=None,
            missing_shards=(),
            errors=(f"local model path is unavailable: {candidate}: {error}",),
        )
    errors: list[str] = []
    if not local_path.is_dir():
        errors.append(f"local model path is not a directory: {local_path}")
    config_path = local_path / "config.json"
    model_type: str | None = None
    if not config_path.is_file():
        errors.append(f"local model has no config.json: {local_path}")
    else:
        try:
            config_value: object = json.loads(config_path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            errors.append(f"cannot read model config {config_path}: {error}")
        else:
            if isinstance(config_value, dict):
                configured_type = config_value.get("model_type")
                if isinstance(configured_type, str):
                    model_type = configured_type
    indexes = sorted(
        [
            *local_path.glob("*.safetensors.index.json"),
            *local_path.glob("*.bin.index.json"),
        ]
    )
    if indexes:
        return _inventory_from_index(
            stripped,
            local_path,
            model_type,
            indexes[0],
            errors,
        )
    return _inventory_without_index(
        stripped,
        local_path,
        model_type,
        errors,
    )


def _inventory_from_index(
    model_argument: str,
    local_path: Path,
    model_type: str | None,
    index_path: Path,
    errors: list[str],
) -> _ModelInventory:
    indexed_weight_bytes: int | None = None
    try:
        index_value: object = json.loads(index_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"cannot read shard index {index_path}: {error}")
        weight_map: dict[str, object] = {}
    else:
        if not isinstance(index_value, dict):
            errors.append(f"shard index must be an object: {index_path}")
            weight_map = {}
        else:
            index_mapping = cast(dict[str, object], index_value)
            raw_weight_map = index_mapping.get("weight_map")
            if not isinstance(raw_weight_map, dict) or not raw_weight_map:
                errors.append(f"shard index has no nonempty weight_map: {index_path}")
                weight_map = {}
            else:
                weight_map = cast(dict[str, object], raw_weight_map)
            raw_metadata = index_mapping.get("metadata")
            if raw_metadata is not None:
                if not isinstance(raw_metadata, dict):
                    errors.append(
                        f"shard index metadata must be an object: {index_path}"
                    )
                else:
                    raw_total_size = raw_metadata.get("total_size")
                    if raw_total_size is not None:
                        if (
                            not isinstance(raw_total_size, int)
                            or isinstance(raw_total_size, bool)
                            or raw_total_size <= 0
                        ):
                            errors.append(
                                "shard index metadata.total_size must be a positive "
                                f"integer: {index_path}"
                            )
                        else:
                            indexed_weight_bytes = raw_total_size
    shard_names = sorted(
        {
            value
            for value in weight_map.values()
            if isinstance(value, str) and value.strip()
        }
    )
    if any(
        not isinstance(value, str) or not value.strip() for value in weight_map.values()
    ):
        errors.append(f"shard index contains invalid paths: {index_path}")
    missing: list[str] = []
    weight_bytes = 0
    for shard_name in shard_names:
        shard_path = (local_path / shard_name).resolve()
        try:
            shard_path.relative_to(local_path)
        except ValueError:
            errors.append(f"shard index escapes the model directory: {shard_name}")
            missing.append(shard_name)
            continue
        if not shard_path.is_file():
            missing.append(shard_name)
            continue
        size = shard_path.stat().st_size
        if size <= 0:
            missing.append(shard_name)
            continue
        if shard_path.suffix == ".safetensors":
            header_error = _safetensors_error(shard_path)
            if header_error is not None:
                errors.append(f"invalid safetensors shard {shard_name}: {header_error}")
                missing.append(shard_name)
                continue
        weight_bytes += size
    if missing:
        errors.append(
            f"model download is incomplete: {len(shard_names) - len(missing)} of "
            f"{len(shard_names)} indexed shards present; {len(missing)} missing"
        )
    elif indexed_weight_bytes is not None and weight_bytes < indexed_weight_bytes:
        errors.append(
            "indexed shard bytes are below metadata.total_size: "
            f"{weight_bytes} < {indexed_weight_bytes}"
        )
    return _ModelInventory(
        model_argument=model_argument,
        local_path=local_path,
        model_type=model_type,
        index_path=index_path,
        weight_bytes=weight_bytes,
        estimated_weight_bytes=(
            indexed_weight_bytes
            if indexed_weight_bytes is not None
            else (
                weight_bytes * len(shard_names) // (len(shard_names) - len(missing))
                if shard_names and len(shard_names) > len(missing)
                else weight_bytes
            )
        ),
        present_shards=len(shard_names) - len(missing),
        expected_shards=len(shard_names),
        missing_shards=tuple(missing),
        errors=tuple(errors),
    )


def _inventory_without_index(
    model_argument: str,
    local_path: Path,
    model_type: str | None,
    errors: list[str],
) -> _ModelInventory:
    safetensors = sorted(local_path.glob("*.safetensors"))
    sharded: list[tuple[Path, re.Match[str]]] = []
    for path in safetensors:
        matched = _SHARD_PATTERN.fullmatch(path.name)
        if matched is None:
            continue
        header_error = _safetensors_error(path)
        if header_error is not None:
            errors.append(f"invalid safetensors shard {path.name}: {header_error}")
            continue
        sharded.append((path, matched))
    if sharded:
        prefix = sharded[0][1].group("prefix")
        total = int(sharded[0][1].group("total"))
        present_parts = {
            int(matched.group("part"))
            for _, matched in sharded
            if matched.group("prefix") == prefix
            and int(matched.group("total")) == total
        }
        missing = tuple(
            f"{prefix}-{part:05d}-of-{total:05d}.safetensors"
            for part in range(1, total + 1)
            if part not in present_parts
        )
        weight_bytes = sum(
            path.stat().st_size
            for path, matched in sharded
            if matched.group("prefix") == prefix
            and int(matched.group("total")) == total
        )
        errors.append(
            "model download is incomplete: missing a shard index and found "
            f"{len(present_parts)} of {total} inferred shards; {len(missing)} missing"
        )
        estimated = (
            weight_bytes * total // len(present_parts)
            if present_parts
            else weight_bytes
        )
        return _ModelInventory(
            model_argument=model_argument,
            local_path=local_path,
            model_type=model_type,
            index_path=None,
            weight_bytes=weight_bytes,
            estimated_weight_bytes=estimated,
            present_shards=len(present_parts),
            expected_shards=total,
            missing_shards=missing,
            errors=tuple(errors),
        )
    unsharded: list[Path] = []
    for path in (
        local_path / "model.safetensors",
        local_path / "pytorch_model.bin",
    ):
        if not path.is_file() or path.stat().st_size <= 0:
            continue
        if path.suffix == ".safetensors":
            header_error = _safetensors_error(path)
            if header_error is not None:
                errors.append(
                    f"invalid safetensors weights {path.name}: {header_error}"
                )
                continue
        unsharded.append(path)
    if not unsharded:
        errors.append(f"local model has no complete weight file: {local_path}")
    weight_bytes = sum(path.stat().st_size for path in unsharded)
    return _ModelInventory(
        model_argument=model_argument,
        local_path=local_path,
        model_type=model_type,
        index_path=None,
        weight_bytes=weight_bytes,
        estimated_weight_bytes=weight_bytes,
        present_shards=len(unsharded),
        expected_shards=len(unsharded),
        missing_shards=(),
        errors=tuple(errors),
    )


def _safetensors_error(path: Path) -> str | None:
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            prefix = stream.read(8)
            if len(prefix) != 8:
                return "missing 8-byte header length"
            header_size = int.from_bytes(prefix, "little", signed=False)
            if header_size > _MAX_SAFETENSORS_HEADER_BYTES:
                return (
                    f"header length {header_size} exceeds "
                    f"{_MAX_SAFETENSORS_HEADER_BYTES}-byte safety limit"
                )
            if header_size <= 1 or 8 + header_size > size:
                return f"header length {header_size} exceeds file size {size}"
            encoded_header = stream.read(header_size)
    except OSError as error:
        return f"cannot read file: {error}"
    try:
        header_value: object = json.loads(encoded_header)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return f"invalid JSON header: {error}"
    if not isinstance(header_value, dict):
        return "header must be an object"
    maximum_data_end = 0
    tensor_count = 0
    for name, descriptor in header_value.items():
        if name == "__metadata__":
            continue
        if not isinstance(descriptor, dict):
            return f"tensor {name!r} descriptor must be an object"
        offsets = descriptor.get("data_offsets")
        if (
            not isinstance(offsets, list)
            or len(offsets) != 2
            or not all(
                isinstance(offset, int) and not isinstance(offset, bool)
                for offset in offsets
            )
        ):
            return f"tensor {name!r} has invalid data_offsets"
        start, end = cast(list[int], offsets)
        if start < 0 or end < start:
            return f"tensor {name!r} has invalid data offset range"
        maximum_data_end = max(maximum_data_end, end)
        tensor_count += 1
    if tensor_count == 0:
        return "header contains no tensors"
    if 8 + header_size + maximum_data_end > size:
        return "tensor data offsets exceed file size"
    return None


def _sysctl_integer(name: str) -> int | None:
    try:
        result = subprocess.run(
            ["/usr/sbin/sysctl", "-n", name],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def _free_memory_percent() -> int | None:
    try:
        result = subprocess.run(
            ["/usr/bin/memory_pressure", "-Q"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    matched = re.search(r"System-wide memory free percentage: (\d+)%", result.stdout)
    if matched is None:
        return None
    percentage = int(matched.group(1))
    return percentage if 0 <= percentage <= 100 else None


def _prepare_model_argument(
    root: Path,
    inventory: _ModelInventory,
    configured_name: str | None,
) -> tuple[str, Path]:
    if inventory.local_path is None:
        if configured_name is not None and configured_name != inventory.model_argument:
            raise ServingError(
                "--served-model-name can only alias a local model directory"
            )
        return inventory.model_argument, root
    served_name = configured_name or _default_model_alias(inventory.local_path)
    _validate_model_alias(served_name)
    alias_root = root / "transformers-models"
    alias_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    alias = alias_root / served_name
    if alias.exists() and not alias.is_symlink():
        raise ServingError(f"model alias path is not a symlink: {alias}")
    temporary = alias_root / f".{served_name}.{os.getpid()}.tmp"
    temporary.unlink(missing_ok=True)
    try:
        temporary.symlink_to(inventory.local_path, target_is_directory=True)
        temporary.replace(alias)
    finally:
        temporary.unlink(missing_ok=True)
    return served_name, alias_root


def _default_model_alias(model_path: Path) -> str:
    if model_path.name.casefold() == "qwen3.8-flash-next":
        return QWEN_TRANSFORMERS_MODEL_ID
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", model_path.name).strip("-.").lower()
    if not slug:
        raise ServingError(f"cannot derive a served model name from {model_path}")
    return f"{slug}-transformers"


def _validate_model_alias(value: str) -> None:
    if _MODEL_ALIAS_PATTERN.fullmatch(value) is None:
        raise ServingError(
            "--served-model-name must contain only letters, digits, dot, dash, "
            "or underscore and be at most 128 characters"
        )


def _transformers_command(
    executable: Path,
    model_argument: str,
    request: ServeRequest,
) -> list[str]:
    command = [
        str(executable),
        "serve",
        "--device",
        request.device,
        "--attn-implementation",
        "sdpa",
        "--host",
        _LOCAL_BIND_HOST,
        "--port",
        str(request.port),
        "--no-continuous-batching",
        "--no-compile",
        "--log-level",
        "info",
    ]
    if request.quantization is not None:
        command.extend(["--quantization", request.quantization])
    command.append(model_argument)
    return command


def _assert_port_available(port: int) -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind((_LOCAL_BIND_HOST, port))
    except OSError as error:
        raise ServingError(
            f"localhost port {port} is unavailable; refusing to validate another server"
        ) from error


def _wait_until_healthy(
    process: subprocess.Popen[str],
    host: str,
    port: int,
    timeout_seconds: float,
) -> tuple[str, ...]:
    deadline = time.monotonic() + timeout_seconds
    last_error = "server did not answer"
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise ServingError(
                f"transformers serve exited before health check (exit {return_code})"
            )
        try:
            return _fetch_models(host, port, timeout_seconds=2.0)
        except ServingError as error:
            last_error = str(error)
        time.sleep(1.0)
    raise ServingError(
        f"transformers serve did not pass /v1/models within "
        f"{timeout_seconds:g}s: {last_error}"
    )


def _fetch_models(host: str, port: int, *, timeout_seconds: float) -> tuple[str, ...]:
    _validate_loopback_host(host)
    opener = build_opener(ProxyHandler({}))
    request = Request(
        _local_http_url(host, port, "/v1/models"),
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            raw = response.read()
    except HTTPError as error:
        raise ServingError(f"/v1/models returned HTTP {error.code}") from error
    except (OSError, URLError) as error:
        raise ServingError(f"/v1/models is unavailable: {error}") from error
    try:
        decoded: object = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ServingError("/v1/models returned invalid JSON") from error
    mapping = _mapping(decoded, "/v1/models response")
    data = mapping.get("data")
    if mapping.get("object") != "list" or not isinstance(data, list):
        raise ServingError("/v1/models returned an invalid model list")
    model_ids: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if isinstance(model_id, str):
            model_ids.append(model_id)
    return tuple(model_ids)


def _terminate_spawned_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10.0)
        return
    except subprocess.TimeoutExpired:
        pass
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5.0)


def _tail_log(path: Path, maximum_bytes: int = 16_384) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - maximum_bytes))
            return stream.read().decode(errors="replace").strip()
    except OSError:
        return ""


def _refresh_state(root: Path, state: _ServeState) -> _ServeState:
    state_path = _state_path(root)
    if not _process_matches(state):
        return _mark_stale_if_needed(state_path, state)
    try:
        models = _fetch_models(state.host, state.port, timeout_seconds=2.0)
    except ServingError as error:
        current = replace(
            state,
            status="unhealthy",
            updated_at=_utc_now(),
            error=str(error),
        )
    else:
        current = replace(
            state,
            status="running",
            updated_at=_utc_now(),
            health_models=models,
            error=None,
        )
    _write_state(state_path, current)
    return current


def _not_configured(root: Path, engine: str) -> dict[str, object]:
    return {
        "schemaVersion": STATE_SCHEMA_VERSION,
        "engine": engine,
        "status": "not-configured",
        "statePath": str(_state_path(root)),
    }


def _mark_stale_if_needed(path: Path, state: _ServeState) -> _ServeState:
    if state.status not in {"starting", "running", "unhealthy"}:
        return state
    stale = replace(
        state,
        status="stale",
        updated_at=_utc_now(),
        error="recorded serving process is no longer running",
    )
    _write_state(path, stale)
    return stale


def _process_matches(state: _ServeState) -> bool:
    try:
        os.kill(state.pid, 0)
    except (PermissionError, ProcessLookupError):
        return False
    identity = _process_identity(state.pid)
    if identity is None:
        return False
    return (
        identity.process_group_id == state.process_group_id
        and state.pid == state.process_group_id
        and identity.birth_token == state.process_birth_token
        and identity.command == state.process_command
    )


def _process_identity(pid: int) -> _ProcessIdentity | None:
    try:
        result = subprocess.run(
            [
                "/bin/ps",
                "-ww",
                "-p",
                str(pid),
                "-o",
                "pgid=",
                "-o",
                "lstart=",
                "-o",
                "command=",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    line = result.stdout.strip()
    if result.returncode != 0 or not line:
        return None
    parts = line.split(maxsplit=6)
    if len(parts) != 7:
        return None
    try:
        process_group_id = int(parts[0])
    except ValueError:
        return None
    return _ProcessIdentity(
        process_group_id=process_group_id,
        birth_token=" ".join(parts[1:6]),
        command=parts[6],
    )


def _wait_for_exit(pid: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.1)
    return False


def _serve_root(workspace: Path) -> Path:
    return workspace.expanduser().resolve() / ".synth" / "serve"


def _local_http_url(host: str, port: int, path: str) -> str:
    rendered_host = f"[{host}]" if ":" in host else host
    return "http" + "://" + f"{rendered_host}:{port}{path}"


def _validate_loopback_host(host: str) -> None:
    if host not in {_LOCAL_BIND_HOST, _PUBLIC_HOST, "::1"}:
        raise ServingError(f"refusing non-loopback serving endpoint host: {host}")


def _state_path(root: Path) -> Path:
    return root / "state.json"


def _log_path(root: Path, engine: str) -> Path:
    return root / f"{engine}.log"


def _read_state(path: Path) -> _ServeState | None:
    if not path.exists():
        return None
    try:
        value: object = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ServingError(f"cannot read serve state {path}: {error}") from error
    return _ServeState.from_object(value, path)


def _write_state(path: Path, state: _ServeState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(state.as_dict(), indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ServingError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ServingError(f"serve state {label} must be a nonempty string")
    return value


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _string(value, label)


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ServingError(f"serve state {label} must be an integer")
    return value
