"""Strict local runtime configuration shared by the bridge and Synth CLI."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from local_memory.observation_redactor import DEFAULT_MAX_TRAJECTORY_TOKENS


class SynthConfigError(ValueError):
    """Raised when synth.json cannot be used safely by the Python control plane."""


EndpointApi = Literal["openai-completions", "openai-responses"]
QwenMode = Literal["thinking", "non-thinking"]
QwenContextProfile = Literal["native", "yarn-1m"]

QWEN_SAMPLING: dict[QwenMode, dict[str, float | int]] = {
    "thinking": {
        "temperature": 1.0,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 0.0,
        "repetition_penalty": 1.0,
    },
    "non-thinking": {
        "temperature": 0.7,
        "top_p": 0.80,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 1.5,
        "repetition_penalty": 1.0,
    },
}

# Model-card YaRN recipe (opt-in):
# Emits an override targeted at text_config.rope_parameters.
# Preserves the shipped keys (mrope_interleaved, mrope_section, partial_rotary_factor, rope_theta)
# and sets rope_type='yarn', factor=4.0, original_max_position_embeddings=262144 (262144 x 4 = 1M).
# Native profile emits nothing (shipped config untouched).
QWEN_YARN_1M_ROPE_OVERRIDE: dict[str, object] = {
    "text_config": {
        "rope_parameters": {
            "mrope_interleaved": True,
            "mrope_section": [11, 11, 10],
            "partial_rotary_factor": 0.25,
            "rope_theta": 10000000,
            "rope_type": "yarn",
            "factor": 4.0,
            "original_max_position_embeddings": 262144,
        }
    }
}


@dataclass(frozen=True, slots=True)
class QwenSettings:
    mode: QwenMode
    context_profile: QwenContextProfile

    @property
    def sampling(self) -> dict[str, float | int]:
        return QWEN_SAMPLING[self.mode]

    @property
    def rope_override(self) -> dict[str, object] | None:
        """Model-card YaRN recipe (opt-in) override targeted at text_config.rope_parameters; native emits None."""
        if self.context_profile == "yarn-1m":
            return QWEN_YARN_1M_ROPE_OVERRIDE
        return None

    @property
    def max_context(self) -> int:
        return 1_048_576 if self.context_profile == "yarn-1m" else 262_144

    def as_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "mode": self.mode,
            "contextProfile": self.context_profile,
            "sampling": self.sampling,
            "maxContext": self.max_context,
        }
        if self.context_profile == "yarn-1m":
            data["ropeOverride"] = {
                "recipe": "model-card YaRN recipe (opt-in)",
                **QWEN_YARN_1M_ROPE_OVERRIDE,
            }
        return data



@dataclass(frozen=True, slots=True)
class EndpointSettings:
    base_url: str
    model: str
    small_model: str
    api_key_env: str
    api: EndpointApi


@dataclass(frozen=True, slots=True)
class TelemetrySettings:
    directory: Path


@dataclass(frozen=True, slots=True)
class MemorySettings:
    enabled: bool
    top_k: int
    min_score: float
    database_path: Path
    embed_model: str
    judge_model: str
    max_trajectory_tokens: int = DEFAULT_MAX_TRAJECTORY_TOKENS


@dataclass(frozen=True, slots=True)
class VerifierSettings:
    interactive: bool
    batch: bool
    max_cycles: int
    continue_generation: bool
    min_budget_fraction: float


@dataclass(frozen=True, slots=True)
class SkillCompilerSettings:
    enabled: bool
    max_per_session: int
    quarantine_path: Path


@dataclass(frozen=True, slots=True)
class SynthSettings:
    workspace: Path
    endpoint: EndpointSettings | None
    telemetry: TelemetrySettings
    memory: MemorySettings
    verifier: VerifierSettings
    skill_compiler: SkillCompilerSettings
    kill_switch: bool
    qwen: QwenSettings | None = None


_TOP_LEVEL_KEYS = {
    "endpoint",
    "telemetry",
    "budgets",
    "memory",
    "verifier",
    "optimizer",
    "skillCompiler",
    "killSwitch",
    "qwen",
}
_ENDPOINT_REQUIRED_KEYS = {"baseUrl", "model", "smallModel", "apiKeyEnv"}
_ENDPOINT_OPTIONAL_KEYS = {"api"}
_TELEMETRY_KEYS = {"dir"}
_MEMORY_REQUIRED_KEYS = {
    "enabled",
    "k",
    "minScore",
    "db",
    "embedModel",
    "judgeModel",
}
_MEMORY_OPTIONAL_KEYS = {"maxTrajectoryTokens"}
_VERIFIER_KEYS = {"interactive", "batch", "maxCycles", "contGen"}
_CONTINUE_GENERATION_KEYS = {"enabled", "minBudgetFrac"}
_SKILL_COMPILER_KEYS = {"enabled", "maxPerSession", "quarantine"}


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
    missing = _ENDPOINT_REQUIRED_KEYS - set(mapping)
    extra = set(mapping) - _ENDPOINT_REQUIRED_KEYS - _ENDPOINT_OPTIONAL_KEYS
    if missing:
        raise SynthConfigError(f"endpoint is missing: {', '.join(sorted(missing))}")
    if extra:
        raise SynthConfigError(
            f"endpoint has unexpected fields: {', '.join(sorted(extra))}"
        )
    api = _string(mapping.get("api", "openai-completions"), "endpoint.api")
    if api not in ("openai-completions", "openai-responses"):
        raise SynthConfigError(
            "endpoint.api must be openai-completions or openai-responses"
        )
    return EndpointSettings(
        base_url=_string(mapping["baseUrl"], "endpoint.baseUrl"),
        model=_string(mapping["model"], "endpoint.model"),
        small_model=_string(mapping["smallModel"], "endpoint.smallModel"),
        api_key_env=_string(mapping["apiKeyEnv"], "endpoint.apiKeyEnv"),
        api=cast(EndpointApi, api),
    )


def _telemetry(workspace: Path, value: object | None) -> TelemetrySettings:
    if value is None:
        return TelemetrySettings(
            directory=resolve_data_plane_path(workspace, ".synth/telemetry")
        )
    mapping = _object(value, "telemetry")
    _exact_keys(mapping, _TELEMETRY_KEYS, "telemetry")
    return TelemetrySettings(
        directory=resolve_data_plane_path(
            workspace, _string(mapping["dir"], "telemetry.dir")
        )
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
            max_trajectory_tokens=DEFAULT_MAX_TRAJECTORY_TOKENS,
        )
    mapping = _object(value, "memory")
    missing = _MEMORY_REQUIRED_KEYS - set(mapping)
    extra = set(mapping) - _MEMORY_REQUIRED_KEYS - _MEMORY_OPTIONAL_KEYS
    if missing:
        raise SynthConfigError(f"memory is missing: {', '.join(sorted(missing))}")
    if extra:
        raise SynthConfigError(
            f"memory has unexpected fields: {', '.join(sorted(extra))}"
        )
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
        max_trajectory_tokens=_integer(
            mapping.get(
                "maxTrajectoryTokens",
                DEFAULT_MAX_TRAJECTORY_TOKENS,
            ),
            "memory.maxTrajectoryTokens",
            minimum=1_000,
            maximum=200_000,
        ),
    )


def _verifier(value: object | None) -> VerifierSettings:
    if value is None:
        return VerifierSettings(
            interactive=False,
            batch=True,
            max_cycles=2,
            continue_generation=True,
            min_budget_fraction=0.2,
        )
    mapping = _object(value, "verifier")
    _exact_keys(mapping, _VERIFIER_KEYS, "verifier")
    continuation = _object(mapping["contGen"], "verifier.contGen")
    _exact_keys(continuation, _CONTINUE_GENERATION_KEYS, "verifier.contGen")
    return VerifierSettings(
        interactive=_boolean(mapping["interactive"], "verifier.interactive"),
        batch=_boolean(mapping["batch"], "verifier.batch"),
        max_cycles=_integer(
            mapping["maxCycles"], "verifier.maxCycles", minimum=0, maximum=10
        ),
        continue_generation=_boolean(
            continuation["enabled"], "verifier.contGen.enabled"
        ),
        min_budget_fraction=_number(
            continuation["minBudgetFrac"],
            "verifier.contGen.minBudgetFrac",
            minimum=0.0,
            maximum=1.0,
        ),
    )


def _skill_compiler(
    workspace: Path, value: object | None, *, enabled_default: bool
) -> SkillCompilerSettings:
    if value is None:
        return SkillCompilerSettings(
            enabled=enabled_default,
            max_per_session=2,
            quarantine_path=resolve_data_plane_path(workspace, ".synth/quarantine"),
        )
    mapping = _object(value, "skillCompiler")
    _exact_keys(mapping, _SKILL_COMPILER_KEYS, "skillCompiler")
    return SkillCompilerSettings(
        enabled=_boolean(mapping["enabled"], "skillCompiler.enabled"),
        max_per_session=_integer(
            mapping["maxPerSession"],
            "skillCompiler.maxPerSession",
            minimum=0,
            maximum=20,
        ),
        quarantine_path=resolve_data_plane_path(
            workspace,
            _string(mapping["quarantine"], "skillCompiler.quarantine"),
        ),
    )


def _qwen(value: object) -> QwenSettings:
    mapping = _object(value, "qwen")
    _exact_keys(mapping, {"mode", "contextProfile"}, "qwen")
    mode = _string(mapping["mode"], "qwen.mode")
    if mode not in ("thinking", "non-thinking"):
        raise SynthConfigError("qwen.mode must be 'thinking' or 'non-thinking'")
    context_profile = _string(mapping["contextProfile"], "qwen.contextProfile")
    if context_profile not in ("native", "yarn-1m"):
        raise SynthConfigError(
            "qwen.contextProfile must be 'native' or 'yarn-1m'"
        )
    return QwenSettings(
        mode=cast(QwenMode, mode),
        context_profile=cast(QwenContextProfile, context_profile),
    )


def load_synth_settings(workspace: Path) -> SynthSettings:
    resolved_workspace = workspace.resolve()
    path = resolved_workspace / "synth.json"
    if not path.exists():
        return SynthSettings(
            workspace=resolved_workspace,
            endpoint=None,
            telemetry=_telemetry(resolved_workspace, None),
            memory=_memory(resolved_workspace, None),
            verifier=_verifier(None),
            skill_compiler=_skill_compiler(
                resolved_workspace, None, enabled_default=False
            ),
            kill_switch=False,
            qwen=None,
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
    qwen = _qwen(mapping["qwen"]) if "qwen" in mapping else None
    return SynthSettings(
        workspace=resolved_workspace,
        endpoint=endpoint,
        telemetry=_telemetry(resolved_workspace, mapping.get("telemetry")),
        memory=_memory(resolved_workspace, mapping.get("memory")),
        verifier=_verifier(mapping.get("verifier")),
        skill_compiler=_skill_compiler(
            resolved_workspace,
            mapping.get("skillCompiler"),
            enabled_default=False,
        ),
        kill_switch=kill_switch,
        qwen=qwen,
    )
