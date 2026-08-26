"""The single Python product entry point for Synth."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast
from uuid import uuid4

from compiler.service import CompileRequest, SkillCompiler, SkillCompilerError
from local_memory.demo import main as _memory_demo_main
from local_memory.embedder import EmbeddingUnavailableError
from local_memory.store import MemoryStoreError
from middleware.batch_driver import run_batch as _run_batch
from middleware.batch_select import select_candidates as _select_candidates
from middleware.telemetry import TelemetryValidationError, validate_telemetry_file
from optimizer.cli import main as optimizer_main
from synth import __version__
from synth.attribution import MTPLX_PRODUCT_ATTRIBUTION
from synth.batch import (
    BatchWiringError,
    default_run_id,
    resolve_task,
    run_bats,
)
from synth.bench import (
    BenchmarkError,
    BenchmarkRequest,
    parse_contexts,
    render_markdown,
    run_benchmark,
)
from synth.bridge import run_bridge
from synth.capabilities import CAPABILITIES
from synth.config import EndpointApi, SynthConfigError, load_synth_settings
from synth.effectiveness import (
    EffectivenessError,
    EndpointSpec,
    run_effectiveness_evaluation,
)
from synth.engine import EngineError
from synth.matts import MattsError, default_matts_run_id, run_matts
from synth.model import ModelCallError, fixture_path_from_environment
from synth.model_pull import (
    DEFAULT_QWEN_GGUF_QUANT,
    ModelPullError,
    Variant,
    pull_model,
)
from synth.ports import prompt_port_manifest
from synth.protocol import JsonObject, JsonValue
from synth.quantize import (
    QuantizeError,
    default_input,
    default_output,
    run_quantize,
)
from synth.reasoning_bank import ReasoningBankError, distill_path, judge_session
from synth.serve import LocalEngineName, ServeError
from synth.serve import start as start_local_server
from synth.service import PROTOCOL_VERSION, BridgeService, ServiceError
from synth.serving import (
    DEVICE_NAMES,
    ENGINE_NAMES,
    QUANTIZATIONS,
    SETUP_ENGINE_NAMES,
    DeviceName,
    Quantization,
    ServeRequest,
    ServingError,
    TransformersEngineName,
    serve_status,
    setup_engine,
    start_server,
    stop_server,
)
from synth.session import SessionParseError

_REVIEWED_ENGINE_ENTRYPOINTS = (_memory_demo_main, _run_batch, _select_candidates)


def _write_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="synth")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("status", help="show implemented and phase-owned capabilities")
    commands.add_parser("ping", help="exercise the in-process bridge service")

    embed = commands.add_parser(
        "embed", help="embed text with the prepared local model"
    )
    embed.add_argument("text", nargs="+", help="one or more texts")
    embed.add_argument("--kind", choices=("document", "query"), default="document")

    commands.add_parser("ports", help="show the research prompt integrity manifest")
    commands.add_parser("optimize", help="run the AutoSaddler outer loop")

    engine = commands.add_parser("engine", help="prepare dedicated serving engines")
    engine_commands = engine.add_subparsers(dest="engine_command", required=True)
    engine_setup = engine_commands.add_parser(
        "setup", help="create or update a serving engine environment"
    )
    engine_setup.add_argument("engine", choices=SETUP_ENGINE_NAMES)

    serve = commands.add_parser("serve", help="manage local model servers")
    serve_commands = serve.add_subparsers(dest="serve_command", required=True)
    serve_start = serve_commands.add_parser(
        "start",
        help="start a local model server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=MTPLX_PRODUCT_ATTRIBUTION,
    )
    serve_start.add_argument("--engine", choices=ENGINE_NAMES, required=True)
    serve_start.add_argument("--model")
    serve_start.add_argument("--quantize", choices=QUANTIZATIONS)
    serve_start.add_argument("--device", choices=DEVICE_NAMES)
    serve_start.add_argument("--port", type=int)
    serve_start.add_argument(
        "--startup-timeout-seconds",
        "--timeout-seconds",
        dest="startup_timeout_seconds",
        type=float,
    )
    serve_start.add_argument("--served-model-name")
    serve_start.add_argument("--host", default="127.0.0.1")
    serve_start.add_argument("--context-profile", choices=("native", "yarn-1m"))
    serve_start.add_argument("--mtplx-checkout", type=Path)
    serve_start.add_argument(
        "--draft-depth",
        type=int,
        help="MTPLX draft depth; 0 selects autoregressive decoding",
    )
    serve_stop = serve_commands.add_parser("stop", help="stop a local model server")
    serve_stop.add_argument("--engine")
    serve_status_parser = serve_commands.add_parser(
        "status", help="show local model server state"
    )
    serve_status_parser.add_argument("--engine")

    bench = commands.add_parser(
        "bench", help="benchmark an OpenAI-compatible endpoint by context depth"
    )
    bench.add_argument("--endpoint", default="http://127.0.0.1:8000/v1")
    bench.add_argument("--model")
    bench.add_argument("--engine", default="openai-compatible")
    bench.add_argument("--quant", default="unknown")
    bench.add_argument(
        "--contexts",
        help="comma-separated ladder subset: 4k,32k,131k,262k,1m",
    )
    bench.add_argument("--try-1m", action="store_true")
    bench.add_argument("--runs", type=int, default=2)
    bench.add_argument("--output-tokens", type=int, default=192)
    bench.add_argument("--timeout-seconds", type=float, default=1800.0)
    bench.add_argument("--seed", type=int, default=38)
    bench.add_argument("--chars-per-token", type=float, default=3.2)
    bench.add_argument("--compare-mtp", action="store_true")
    bench.add_argument("--mtp-depth", type=int, default=3)
    bench.add_argument("--api-key-env", default="SYNTH_LOCAL_API_KEY")
    bench.add_argument("--run-id")

    run = commands.add_parser("run", help="run a coding task through Synth")
    run.add_argument("--task", required=True, help="task text or a task-spec path")
    run.add_argument("--bats", action="store_true", help="enable BATS batch scaling")
    run.add_argument("--target", type=Path, default=Path.cwd())
    run.add_argument("--max-trajectories", type=int, default=3)
    run.add_argument("--base-ref", default="HEAD")
    run.add_argument("--run-id")
    run.add_argument("--timeout-seconds", type=float, default=900.0)
    run.add_argument("--fixture-cli", type=Path, help=argparse.SUPPRESS)
    run.add_argument("--judge-fixture", type=Path, help=argparse.SUPPRESS)

    evaluate = commands.add_parser(
        "evaluate", help="compare ReasoningBank memory on versus off"
    )
    evaluate.add_argument("--tasks", type=Path, default=Path("tests/harness_tasks/dev"))
    evaluate.add_argument("--case", action="append", default=[])
    evaluate.add_argument("--bank", type=Path, required=True)
    evaluate.add_argument("--model", default="local/local-main")
    evaluate.add_argument("--thinking", default="off")
    evaluate.add_argument("--repetitions", type=int, default=1)
    evaluate.add_argument("--run-id")
    evaluate.add_argument("--timeout-seconds", type=float, default=900.0)
    evaluate.add_argument("--endpoint-base-url", default="http://localhost:8000/v1")
    evaluate.add_argument(
        "--endpoint-api",
        choices=("openai-completions", "openai-responses"),
        default="openai-completions",
    )
    evaluate.add_argument("--endpoint-model", default="local-small")
    evaluate.add_argument("--endpoint-api-key-env", default="SYNTH_LOCAL_API_KEY")

    matts = commands.add_parser(
        "matts", help="run memory-aware parallel and sequential scaling"
    )
    matts.add_argument("--task", required=True, help="task text or a task-spec path")
    matts.add_argument("-k", type=int, required=True, help="parallel trajectories")
    matts.add_argument("--target", type=Path, default=Path.cwd())
    matts.add_argument("--base-ref", default="HEAD")
    matts.add_argument("--run-id")
    matts.add_argument("--timeout-seconds", type=float, default=900.0)
    matts.add_argument("--fixture-cli", type=Path, help=argparse.SUPPRESS)
    matts.add_argument("--model-fixture", type=Path, help=argparse.SUPPRESS)

    memory = commands.add_parser("memory", help="inspect ReasoningBank memory")
    memory_commands = memory.add_subparsers(dest="memory_command", required=True)
    memory_list = memory_commands.add_parser("list", help="list stored memory items")
    memory_list.add_argument("--limit", type=int, default=50)
    memory_search = memory_commands.add_parser(
        "search", help="search stored memory items"
    )
    memory_search.add_argument("query", nargs="+")
    memory_search.add_argument("-k", type=int)
    memory_search.add_argument("--min-score", type=float)

    judge = commands.add_parser("judge", help="judge a persisted v3 trajectory")
    judge.add_argument("session", type=Path)
    judge.add_argument("--exit-code", type=int)
    judge.add_argument("--fixture", type=Path)

    distill = commands.add_parser(
        "distill", help="judge and distill a session or session directory"
    )
    distill.add_argument("session", type=Path)
    distill.add_argument("--exit-code", type=int)
    distill.add_argument("--fixture", type=Path)

    compile_command = commands.add_parser(
        "compile", help="compile a recurring session workflow into a gated skill"
    )
    compile_command.add_argument("--from", dest="session", type=Path, required=True)
    compile_command.add_argument("--workflow")
    compile_command.add_argument("--name-hint")
    compile_command.add_argument(
        "--reason", default="Recurring workflow selected for reuse"
    )
    compile_command.add_argument("--actor", default="synth-cli")
    compile_command.add_argument("--request-id")
    compile_command.add_argument("--fixture", type=Path, help=argparse.SUPPRESS)

    skill = commands.add_parser("skill", help="manage compiler-promoted skills")
    skill_commands = skill.add_subparsers(dest="skill_command", required=True)
    demote = skill_commands.add_parser(
        "demote", help="move a compiler-managed skill into recoverable storage"
    )
    demote.add_argument("name")
    demote.add_argument("--reason", default="User-requested demotion")

    telemetry = commands.add_parser("telemetry", help="read extension telemetry")
    telemetry_commands = telemetry.add_subparsers(
        dest="telemetry_command", required=True
    )
    validate = telemetry_commands.add_parser("validate", help="validate a JSONL stream")
    validate.add_argument("path", type=Path)

    model = commands.add_parser("model", help="manage local model artifacts")
    model_commands = model.add_subparsers(dest="model_command", required=True)
    pull = model_commands.add_parser(
        "pull", help="pull model weights from Hugging Face"
    )
    pull.add_argument(
        "name",
        nargs="?",
        default="qwen3.8-flash-next",
        help="model name or HF repo ID (default: qwen3.8-flash-next)",
    )
    pull.add_argument(
        "--variant",
        choices=("bf16", "mlx", "gguf"),
        default="bf16",
        help="model weight format variant (default: bf16 = Qwen/Qwen3.8-Flash-Next)",
    )
    pull.add_argument(
        "--target",
        type=Path,
        default=None,
        help="destination directory (default: ~/models/<repo-name>)",
    )
    pull.add_argument(
        "--gguf-quant",
        default=None,
        help=(
            "GGUF quantization directory to download "
            f"(default with --variant gguf: {DEFAULT_QWEN_GGUF_QUANT})"
        ),
    )
    pull.add_argument(
        "--dry-run",
        action="store_true",
        help="print download plan and next steps without downloading",
    )
    quantize = model_commands.add_parser(
        "quantize", help="convert bf16 weights to mixed46 MLX"
    )
    quantize.add_argument("--input", type=Path, default=default_input())
    quantize.add_argument("--output", type=Path, default=default_output())
    quantize.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resolved recipe without converting",
    )

    commands.add_parser("config", help="show resolved synth configuration")

    commands.add_parser("bridge", help=argparse.SUPPRESS)
    return parser


def _service() -> BridgeService:
    return BridgeService(Path.cwd())


def _normalize_serve_shorthand(arguments: list[str]) -> list[str]:
    normalized = list(arguments)
    explicit_subcommands = {"start", "status", "stop"}
    if (
        normalized[:1] == ["serve"]
        and "--engine" in normalized[1:]
        and (len(normalized) < 2 or normalized[1] not in explicit_subcommands)
    ):
        normalized.insert(1, "start")
    return normalized


def _service_request(command: str, params: JsonObject) -> JsonObject:
    service = _service()
    try:
        return service.handle(command, params)
    finally:
        service.close()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _normalize_serve_shorthand(
        list(argv) if argv is not None else sys.argv[1:]
    )
    if arguments[:1] == ["optimize"]:
        optimizer_main(arguments)
        return 0
    args = _parser().parse_args(arguments)
    command = cast(str, args.command)
    try:
        if command == "status":
            settings = load_synth_settings(Path.cwd())
            _write_json(
                {
                    "version": __version__,
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": [item.as_dict() for item in CAPABILITIES],
                    "promptPorts": len(prompt_port_manifest()),
                    "qwen": settings.qwen.as_dict() if settings.qwen else None,
                }
            )
            return 0
        if command == "ping":
            _write_json(_service_request("ping", {"protocolVersion": PROTOCOL_VERSION}))
            return 0
        if command == "embed":
            texts = cast(list[str], args.text)
            kind = cast(str, args.kind)
            params: JsonObject = {
                "texts": cast(JsonValue, texts),
                "kind": kind,
            }
            _write_json(_service_request("embed", params))
            return 0
        if command == "ports":
            _write_json(prompt_port_manifest())
            return 0
        if command == "engine":
            engine_command = cast(str, args.engine_command)
            if engine_command != "setup":
                raise ValueError(f"unknown engine command: {engine_command}")
            _write_json(setup_engine(cast(TransformersEngineName, args.engine)))
            return 0
        if command == "serve":
            serve_command = cast(str, args.serve_command)
            if serve_command == "start":
                selected = cast(str, args.engine)
                model = cast(str | None, args.model)
                configured_port = cast(int | None, args.port)
                configured_timeout = cast(float | None, args.startup_timeout_seconds)
                if selected == "transformers":
                    if model is None:
                        raise ValueError("--model is required for transformers")
                    if cast(str, args.host) != "127.0.0.1":
                        raise ValueError("transformers only supports --host 127.0.0.1")
                    if cast(str | None, args.context_profile) is not None:
                        raise ValueError(
                            "--context-profile is only supported by llama.cpp"
                        )
                    _write_json(
                        start_server(
                            Path.cwd(),
                            ServeRequest(
                                engine="transformers",
                                model=model,
                                quantization=cast(Quantization | None, args.quantize),
                                device=cast(DeviceName | None, args.device) or "mps",
                                port=(
                                    configured_port
                                    if configured_port is not None
                                    else 8000
                                ),
                                startup_timeout_seconds=(
                                    configured_timeout
                                    if configured_timeout is not None
                                    else 900.0
                                ),
                                served_model_name=cast(
                                    str | None, args.served_model_name
                                ),
                            ),
                        )
                    )
                    return 0
                if cast(Quantization | None, args.quantize) is not None:
                    raise ValueError("--quantize is only supported by transformers")
                if cast(DeviceName | None, args.device) is not None:
                    raise ValueError("--device is only supported by transformers")
                mtplx_checkout = cast(Path | None, args.mtplx_checkout)
                draft_depth = cast(int | None, args.draft_depth)
                if selected != "mtplx" and mtplx_checkout is not None:
                    raise ValueError("--mtplx-checkout is only supported by mtplx")
                if selected != "mtplx" and draft_depth is not None:
                    raise ValueError("--draft-depth is only supported by mtplx")
                if (
                    selected == "mlx"
                    and cast(str | None, args.context_profile) is not None
                ):
                    raise ValueError("--context-profile is only supported by llama.cpp")
                _write_json(
                    start_local_server(
                        Path.cwd(),
                        engine=cast(LocalEngineName, selected),
                        model=Path(model) if model is not None else None,
                        port=(
                            configured_port
                            if configured_port is not None
                            else 8000
                            if selected == "mtplx"
                            else 8080
                        ),
                        host=cast(str, args.host),
                        context_profile=cast(str | None, args.context_profile),
                        alias=cast(str | None, args.served_model_name),
                        timeout_seconds=(
                            configured_timeout
                            if configured_timeout is not None
                            else 300.0
                        ),
                        mtplx_checkout=mtplx_checkout,
                        draft_depth=draft_depth if draft_depth is not None else 3,
                    )
                )
                return 0
            selected_engine = cast(str | None, args.engine)
            if serve_command == "stop":
                _write_json(stop_server(Path.cwd(), selected_engine))
                return 0
            if serve_command == "status":
                _write_json(serve_status(Path.cwd(), selected_engine))
                return 0
            raise ValueError(f"unknown serve command: {serve_command}")
        if command == "bench":
            report = run_benchmark(
                Path.cwd(),
                BenchmarkRequest(
                    endpoint=cast(str, args.endpoint),
                    model=cast(str | None, args.model),
                    engine=cast(str, args.engine),
                    quant=cast(str, args.quant),
                    contexts=parse_contexts(cast(str | None, args.contexts)),
                    try_1m=cast(bool, args.try_1m),
                    runs=cast(int, args.runs),
                    output_tokens=cast(int, args.output_tokens),
                    timeout_seconds=cast(float, args.timeout_seconds),
                    seed=cast(int, args.seed),
                    chars_per_token=cast(float, args.chars_per_token),
                    compare_mtp=cast(bool, args.compare_mtp),
                    mtp_depth=cast(int, args.mtp_depth),
                    api_key_env=cast(str, args.api_key_env),
                    run_id=cast(str | None, args.run_id),
                ),
            )
            print(render_markdown(report), end="")
            return 0 if report.get("complete") is True else 1
        if command == "run":
            if not cast(bool, args.bats):
                raise ValueError("synth run currently requires --bats")
            target = cast(Path, args.target).expanduser().resolve()
            task = resolve_task(cast(str, args.task), target)
            configured_run_id = cast(str | None, args.run_id)
            _write_json(
                run_bats(
                    workspace=target,
                    task=task,
                    max_trajectories=cast(int, args.max_trajectories),
                    base_ref=cast(str, args.base_ref),
                    run_id=configured_run_id or default_run_id(),
                    timeout_seconds=cast(float, args.timeout_seconds),
                    fixture_cli=cast(Path | None, args.fixture_cli),
                    judge_fixture=cast(Path | None, args.judge_fixture),
                )
            )
            return 0
        if command == "evaluate":
            selected_cases = tuple(cast(list[str], args.case))
            endpoint_api = cast(str, args.endpoint_api)
            if endpoint_api not in ("openai-completions", "openai-responses"):
                raise ValueError(f"unknown endpoint API: {endpoint_api}")
            evaluation = run_effectiveness_evaluation(
                source_root=Path.cwd(),
                tasks_root=cast(Path, args.tasks),
                bank_path=cast(Path, args.bank),
                model=cast(str, args.model),
                thinking=cast(str, args.thinking),
                endpoint=EndpointSpec(
                    base_url=cast(str, args.endpoint_base_url),
                    api=cast(EndpointApi, endpoint_api),
                    model=cast(str, args.endpoint_model),
                    api_key_env=cast(str, args.endpoint_api_key_env),
                ),
                repetitions=cast(int, args.repetitions),
                selected_cases=selected_cases,
                run_id=cast(str | None, args.run_id),
                timeout_seconds=cast(float, args.timeout_seconds),
            )
            _write_json(evaluation)
            return 0 if evaluation.get("complete") is True else 1
        if command == "matts":
            target = cast(Path, args.target).expanduser().resolve()
            task = resolve_task(cast(str, args.task), target)
            configured_run_id = cast(str | None, args.run_id)
            _write_json(
                run_matts(
                    workspace=target,
                    task=task,
                    k=cast(int, args.k),
                    base_ref=cast(str, args.base_ref),
                    run_id=configured_run_id or default_matts_run_id(),
                    timeout_seconds=cast(float, args.timeout_seconds),
                    fixture_cli=cast(Path | None, args.fixture_cli),
                    model_fixture=cast(Path | None, args.model_fixture),
                )
            )
            return 0
        if command == "telemetry":
            telemetry_command = cast(str, args.telemetry_command)
            if telemetry_command != "validate":
                raise ValueError(f"unknown telemetry command: {telemetry_command}")
            path = cast(Path, args.path)
            result = validate_telemetry_file(path)
            _write_json({"events": result.events, "sessions": result.sessions})
            return 0
        if command == "memory":
            memory_command = cast(str, args.memory_command)
            if memory_command == "list":
                _write_json(
                    _service_request("memory.list", {"limit": cast(int, args.limit)})
                )
                return 0
            if memory_command == "search":
                settings = load_synth_settings(Path.cwd())
                query = " ".join(cast(list[str], args.query))
                top_k = cast(int | None, args.k) or settings.memory.top_k
                configured_min_score = cast(float | None, args.min_score)
                min_score = (
                    settings.memory.min_score
                    if configured_min_score is None
                    else configured_min_score
                )
                _write_json(
                    _service_request(
                        "memory.query",
                        {
                            "query": query,
                            "taskDescription": (
                                "Given prior software engineering memories, analyze "
                                "the current task's intent and select relevant "
                                "strategies or lessons that could help resolve it."
                            ),
                            "k": top_k,
                            "minScore": min_score,
                            "statuses": ["success", "fail"],
                            "itemTypes": ["strategy"],
                        },
                    )
                )
                return 0
            raise ValueError(f"unknown memory command: {memory_command}")
        if command == "judge":
            fixture = cast(Path | None, args.fixture) or fixture_path_from_environment()
            judged = judge_session(
                cast(Path, args.session),
                exit_code=cast(int | None, args.exit_code),
                fixture_path=fixture,
                wait_for_stability=True,
            )
            _write_json(judged.as_dict())
            return 0
        if command == "distill":
            fixture = cast(Path | None, args.fixture) or fixture_path_from_environment()
            _write_json(
                distill_path(
                    cast(Path, args.session),
                    exit_code=cast(int | None, args.exit_code),
                    fixture_path=fixture,
                    wait_for_stability=True,
                )
            )
            return 0
        if command == "compile":
            configured_request_id = cast(str | None, args.request_id)
            compile_outcome = SkillCompiler().compile(
                CompileRequest(
                    session_path=cast(Path, args.session),
                    request_id=configured_request_id or str(uuid4()),
                    actor=cast(str, args.actor),
                    reason=cast(str, args.reason),
                    workflow=cast(str | None, args.workflow),
                    name_hint=cast(str | None, args.name_hint),
                    fixture_path=cast(Path | None, args.fixture),
                )
            )
            _write_json(compile_outcome.as_dict())
            return 0 if compile_outcome.status == "promoted" else 1
        if command == "skill":
            skill_command = cast(str, args.skill_command)
            if skill_command != "demote":
                raise ValueError(f"unknown skill command: {skill_command}")
            demotion_outcome = SkillCompiler().demote(
                Path.cwd(),
                cast(str, args.name),
                actor="synth-cli",
                reason=cast(str, args.reason),
            )
            _write_json(demotion_outcome.as_dict())
            return 0
        if command == "model":
            model_command = cast(str, args.model_command)
            if model_command == "pull":
                pull_model(
                    model_name=cast(str, args.name),
                    variant=cast(Variant, args.variant),
                    target_dir=cast(Path | None, args.target),
                    dry_run=cast(bool, args.dry_run),
                    gguf_quant=cast(str | None, args.gguf_quant),
                )
                return 0
            if model_command == "quantize":
                return run_quantize(
                    cast(Path, args.input),
                    cast(Path, args.output),
                    dry_run=cast(bool, args.dry_run),
                )
            raise ValueError(f"unknown model command: {model_command}")
        if command == "config":
            settings = load_synth_settings(Path.cwd())
            _write_json(
                {
                    "endpoint": (
                        {
                            "baseUrl": settings.endpoint.base_url,
                            "model": settings.endpoint.model,
                            "smallModel": settings.endpoint.small_model,
                            "apiKeyEnv": settings.endpoint.api_key_env,
                            "api": settings.endpoint.api,
                        }
                        if settings.endpoint
                        else None
                    ),
                    "qwen": settings.qwen.as_dict() if settings.qwen else None,
                    "telemetry": {"dir": str(settings.telemetry.directory)},
                    "memory": {
                        "enabled": settings.memory.enabled,
                        "k": settings.memory.top_k,
                        "minScore": settings.memory.min_score,
                        "db": str(settings.memory.database_path),
                        "embedModel": settings.memory.embed_model,
                        "judgeModel": settings.memory.judge_model,
                    },
                    "verifier": {
                        "interactive": settings.verifier.interactive,
                        "batch": settings.verifier.batch,
                        "maxCycles": settings.verifier.max_cycles,
                        "contGen": {
                            "enabled": settings.verifier.continue_generation,
                            "minBudgetFrac": settings.verifier.min_budget_fraction,
                        },
                    },
                    "skillCompiler": {
                        "enabled": settings.skill_compiler.enabled,
                        "maxPerSession": settings.skill_compiler.max_per_session,
                        "quarantine": str(settings.skill_compiler.quarantine_path),
                    },
                    "killSwitch": {"all": settings.kill_switch},
                }
            )
            return 0
        if command == "bridge":
            return run_bridge()
        raise ValueError(f"unknown command: {command}")
    except (
        EmbeddingUnavailableError,
        BatchWiringError,
        BenchmarkError,
        EffectivenessError,
        EngineError,
        MattsError,
        MemoryStoreError,
        ModelCallError,
        ModelPullError,
        OSError,
        QuantizeError,
        ReasoningBankError,
        ServeError,
        ServiceError,
        SessionParseError,
        SkillCompilerError,
        ServingError,
        SynthConfigError,
        TelemetryValidationError,
        ValueError,
    ) as error:
        print(f"synth: {error}", file=sys.stderr)
        return 2


def bridge_main() -> int:
    return run_bridge()


if __name__ == "__main__":
    raise SystemExit(main())
