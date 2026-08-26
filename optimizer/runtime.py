from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from optimizer.synth_harness import (
    build_synth_harness_components,
    parse_synth_harness_settings,
)
from optimizer.transport import (
    PrimeAgentProvider,
    PrimeAgentTransport,
    PrimeAgentTransportConfig,
    StubPrimeAgentTransport,
    find_node,
)
from optimizer.vendor.autosaddler_v2.config.models import RunConfig
from optimizer.vendor.autosaddler_v2.core.domain import JsonValue
from optimizer.vendor.autosaddler_v2.core.engine import AutoSaddlerEngine
from optimizer.vendor.autosaddler_v2.core.policies import (
    BudgetPolicy,
    EpochShuffledTaskSelectionPolicy,
    FixedTaskSelectionPolicy,
    FullOnAcceptDevelopment,
    MatchedValidStrictImprovement,
    MeanDevelopmentRanking,
    PolicyBundle,
)
from optimizer.vendor.autosaddler_v2.core.ports import ScenarioComponents
from optimizer.vendor.autosaddler_v2.providers.base import AgentTransport
from optimizer.vendor.autosaddler_v2.storage.local import LocalRunStore


@dataclass(frozen=True, slots=True)
class SynthRuntime:
    config: RunConfig
    store: LocalRunStore
    scenario: ScenarioComponents
    transport: AgentTransport
    engine: AutoSaddlerEngine


def build_synth_runtime(config_path: Path, *, run_id: str) -> SynthRuntime:
    if not run_id or "/" in run_id or ".." in run_id:
        raise ValueError("run_id must be one safe path segment")
    resolved_path = config_path.resolve(strict=True)
    config = RunConfig.load(resolved_path)
    if config.scenario.type != "synth_harness":
        raise ValueError(f"Unsupported Synth scenario: {config.scenario.type}")
    if config.storage.type != "local":
        raise ValueError(f"Unsupported Synth storage: {config.storage.type}")
    run_dir = config.storage.run_root / run_id
    store = LocalRunStore(run_dir=run_dir, run_id=run_id)
    transport = _build_transport(
        config.provider.type,
        config.provider.settings,
        base_dir=resolved_path.parent,
    )
    scenario_settings = parse_synth_harness_settings(
        config.scenario.settings,
        base_dir=resolved_path.parent,
    )
    scenario = build_synth_harness_components(
        settings=scenario_settings,
        run_dir=run_dir,
        store=store,
        transport=transport,
    )
    missing_capabilities = sorted(
        scenario.required_capabilities - config.provider.capabilities
    )
    if missing_capabilities:
        raise ValueError(
            f"Configured provider lacks required capabilities: {missing_capabilities}"
        )
    policies = PolicyBundle(
        task_selection=_task_selection(config),
        acceptance=MatchedValidStrictImprovement(),
        development=FullOnAcceptDevelopment(),
        ranking=MeanDevelopmentRanking(),
        budget=BudgetPolicy(
            max_rollouts=config.optimization.budget.max_rollouts,
            max_iterations=config.optimization.budget.max_iterations,
        ),
    )
    if config.optimization.acceptance.type != "matched_valid_strict_improvement":
        raise ValueError("Synth requires matched_valid_strict_improvement acceptance")
    if config.optimization.development.type != "full_on_accept":
        raise ValueError("Synth requires full_on_accept development evaluation")
    if config.optimization.ranking.type != "mean_development_score":
        raise ValueError("Synth requires mean_development_score ranking")
    store.initialize(
        resolved_config=config.as_mapping(),
        resolved_entities={
            "resolved/component_graph.json": {
                "schema_version": "synth-autosaddler-component-graph/v1",
                "scenario": "synth_harness",
                "provider": config.provider.type,
                "components": [
                    "GitHarnessSpace",
                    "SynthHarnessEvaluator",
                    "SynthHarnessPromptPack",
                    "PrimeAgentTransport",
                ],
            },
            "resolved/provider_runtime.json": {
                "schema_version": "synth-prime-agent-provider/v1",
                "provider_type": config.provider.type,
                "settings": dict(config.provider.settings),
            },
            "resolved/policies.json": {
                "task_selection": config.optimization.task_selection.type,
                "batch_size": config.optimization.task_selection.batch_size,
                "acceptance": config.optimization.acceptance.type,
                "development": config.optimization.development.type,
                "ranking": config.optimization.ranking.type,
                "budget": {
                    "max_rollouts": policies.budget.max_rollouts,
                    "max_iterations": policies.budget.max_iterations,
                },
            },
            **scenario.resolved_entities,
        },
    )
    provider = PrimeAgentProvider(transport)
    engine = AutoSaddlerEngine(
        store=store,
        scenario=scenario,
        provider=provider,
        policies=policies,
        diagnosis_patch_timeout_seconds=(
            config.optimization.diagnosis_patch_timeout_seconds
        ),
        selection_timeout_seconds=config.optimization.selection_timeout_seconds,
        reflection_timeout_seconds=config.optimization.reflection_timeout_seconds,
        session_retries=config.optimization.session_retries,
        session_retry_backoff_seconds=(
            config.optimization.session_retry_backoff_seconds
        ),
    )
    return SynthRuntime(config, store, scenario, transport, engine)


def _task_selection(
    config: RunConfig,
) -> FixedTaskSelectionPolicy | EpochShuffledTaskSelectionPolicy:
    selection = config.optimization.task_selection
    if selection.type == "fixed":
        return FixedTaskSelectionPolicy(batch_size=selection.batch_size)
    if selection.type == "epoch_shuffled":
        return EpochShuffledTaskSelectionPolicy(
            batch_size=selection.batch_size,
            seed=selection.seed,
        )
    raise ValueError(f"Unsupported task selection policy: {selection.type}")


def _build_transport(
    provider_type: str,
    settings: Mapping[str, JsonValue],
    *,
    base_dir: Path,
) -> AgentTransport:
    if provider_type == "stub":
        expected = {"target_path", "improved_skill_file"}
        _exact_settings(settings, expected, provider_type)
        improved_path = _resolve_file(
            base_dir,
            settings["improved_skill_file"],
            "provider.settings.improved_skill_file",
        )
        return StubPrimeAgentTransport(
            target_path=_string(
                settings["target_path"],
                "provider.settings.target_path",
            ),
            improved_text=improved_path.read_text(encoding="utf-8"),
        )
    if provider_type == "prime_agent":
        expected = {
            "cli_path",
            "endpoint",
            "model",
            "api_key_env",
            "node_binary",
        }
        _exact_settings(settings, expected, provider_type)
        node_binary = find_node(
            _string(settings["node_binary"], "provider.settings.node_binary")
        )
        return PrimeAgentTransport(
            PrimeAgentTransportConfig(
                cli_path=_resolve_file(
                    base_dir,
                    settings["cli_path"],
                    "provider.settings.cli_path",
                ),
                endpoint=_string(
                    settings["endpoint"],
                    "provider.settings.endpoint",
                ),
                model=_string(settings["model"], "provider.settings.model"),
                api_key_env=_string(
                    settings["api_key_env"],
                    "provider.settings.api_key_env",
                ),
                node_binary=node_binary,
            )
        )
    raise ValueError(f"Unsupported Synth provider: {provider_type}")


def _exact_settings(
    settings: Mapping[str, JsonValue],
    expected: set[str],
    provider_type: str,
) -> None:
    missing = sorted(expected - settings.keys())
    extra = sorted(settings.keys() - expected)
    if missing or extra:
        raise ValueError(
            f"Invalid {provider_type} provider settings: "
            f"missing={missing}, extra={extra}"
        )


def _resolve_file(base_dir: Path, value: JsonValue, label: str) -> Path:
    raw = _string(value, label)
    path = Path(raw)
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve(strict=True)


def _string(value: JsonValue, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"{label} must be a non-empty string")
    return value
