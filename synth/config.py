"""Strict local runtime configuration shared by the bridge and Synth CLI."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast


class SynthConfigError(ValueError):
    """Raised when synth.json cannot be used safely by the Python control plane."""


@dataclass(frozen=True, slots=True)
class EndpointSettings:
    base_url: str
    model: str
    small_model: str
    api_key_env: str


@dataclass(frozen=True, slots=True)
class MemorySettings:
    enabled: bool
    top_k: int
    min_score: float
    database_path: Path
    embed_model: str
    judge_model: str


@dataclass(frozen=True, slots=True)
class SynthSettings:
    workspace: Path
    endpoint: EndpointSettings | None
    memory: MemorySettings
    kill_switch: bool


_TOP_LEVEL_KEYS = {
    "endpoint",
    "telemetry",
    "budgets",
    "memory",
    "verifier",
    "optimizer",
    "skillCompiler",
    "killSwitch",
}
_ENDPOINT_KEYS = {"baseUrl", "model", "smallModel", "apiKeyEnv"}
_MEMORY_KEYS = {"enabled", "k", "minScore", "db", "embedModel", "judgeModel"}


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SynthConfigError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _exact_keys(value: dict[str, object], expected: set[str], label: str) -> None:
    missing = expected - set(value)
    extra = set(value) - expected
    if missing:
        raise SynthConfigError(f"{label} is missing: {', '.join(sorted(missing))}")
    if extra:
        raise SynthConfigError(
            f"{label} has unexpected fields: {', '.join(sorted(extra))}"
        )


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SynthConfigError(f"{label} must be a nonempty string")
    return value.strip()


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise SynthConfigError(f"{label} must be a boolean")
    return value


def _integer(value: object, label: str, *, minimum: int, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise SynthConfigError(
            f"{label} must be an integer from {minimum} to {maximum}"
        )
    return value


def _number(value: object, label: str, *, minimum: float, maximum: float) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not minimum <= float(value) <= maximum
    ):
        raise SynthConfigError(f"{label} must be between {minimum} and {maximum}")
    return float(value)


def resolve_data_plane_path(workspace: Path, configured_path: str) -> Path:
    root = (workspace / ".synth").resolve()
    candidate = (workspace / configured_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise SynthConfigError(
            f"data-plane path must remain inside {root}: {configured_path}"
        ) from error
    return candidate


def _endpoint(value: object) -> EndpointSettings:
    mapping = _object(value, "endpoint")
    _exact_keys(mapping, _ENDPOINT_KEYS, "endpoint")
    return EndpointSettings(
        base_url=_string(mapping["baseUrl"], "endpoint.baseUrl"),
        model=_string(mapping["model"], "endpoint.model"),
        small_model=_string(mapping["smallModel"], "endpoint.smallModel"),
        api_key_env=_string(mapping["apiKeyEnv"], "endpoint.apiKeyEnv"),
    )


def _memory(workspace: Path, value: object | None) -> MemorySettings:
    if value is None:
        return MemorySettings(
            enabled=False,
            top_k=3,
            min_score=0.35,
            database_path=resolve_data_plane_path(workspace, ".synth/synth.db"),
            embed_model="BAAI/bge-small-en-v1.5",
            judge_model="small",
        )
    mapping = _object(value, "memory")
    _exact_keys(mapping, _MEMORY_KEYS, "memory")
    database = _string(mapping["db"], "memory.db")
    return MemorySettings(
        enabled=_boolean(mapping["enabled"], "memory.enabled"),
        top_k=_integer(mapping["k"], "memory.k", minimum=1, maximum=20),
        min_score=_number(
            mapping["minScore"], "memory.minScore", minimum=0.0, maximum=1.0
        ),
        database_path=resolve_data_plane_path(workspace, database),
        embed_model=_string(mapping["embedModel"], "memory.embedModel"),
        judge_model=_string(mapping["judgeModel"], "memory.judgeModel"),
    )


def load_synth_settings(workspace: Path) -> SynthSettings:
    resolved_workspace = workspace.resolve()
    path = resolved_workspace / "synth.json"
    if not path.exists():
        return SynthSettings(
            workspace=resolved_workspace,
            endpoint=None,
            memory=_memory(resolved_workspace, None),
            kill_switch=False,
        )
    if not path.is_file():
        raise SynthConfigError(f"config is not a regular file: {path}")
    try:
        raw: object = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SynthConfigError(f"cannot read {path}: {error}") from error
    mapping = _object(raw, "synth.json")
    extra = set(mapping) - _TOP_LEVEL_KEYS
    if extra:
        raise SynthConfigError(
            f"synth.json has unexpected fields: {', '.join(sorted(extra))}"
        )
    kill_switch = False
    if "killSwitch" in mapping:
        kill_switch_mapping = _object(mapping["killSwitch"], "killSwitch")
        _exact_keys(kill_switch_mapping, {"all"}, "killSwitch")
        kill_switch = _boolean(kill_switch_mapping["all"], "killSwitch.all")
    endpoint = _endpoint(mapping["endpoint"]) if "endpoint" in mapping else None
    return SynthSettings(
        workspace=resolved_workspace,
        endpoint=endpoint,
        memory=_memory(resolved_workspace, mapping.get("memory")),
        kill_switch=kill_switch,
    )
