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
from synth.batch import (
    BatchWiringError,
    default_run_id,
    resolve_task,
    run_bats,
)
from synth.bridge import run_bridge
from synth.capabilities import CAPABILITIES
from synth.config import SynthConfigError, load_synth_settings
from synth.model import ModelCallError, fixture_path_from_environment
from synth.ports import prompt_port_manifest
from synth.protocol import JsonObject, JsonValue
from synth.reasoning_bank import ReasoningBankError, distill_path, judge_session
from synth.service import PROTOCOL_VERSION, BridgeService, ServiceError
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

    commands.add_parser("bridge", help=argparse.SUPPRESS)
    return parser


def _service() -> BridgeService:
    return BridgeService(Path.cwd())


def _service_request(command: str, params: JsonObject) -> JsonObject:
    service = _service()
    try:
        return service.handle(command, params)
    finally:
        service.close()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    if arguments[:1] == ["optimize"]:
        optimizer_main(arguments)
        return 0
    args = _parser().parse_args(arguments)
    command = cast(str, args.command)
    try:
        if command == "status":
            _write_json(
                {
                    "version": __version__,
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": [item.as_dict() for item in CAPABILITIES],
                    "promptPorts": len(prompt_port_manifest()),
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
        if command == "bridge":
            return run_bridge()
        raise ValueError(f"unknown command: {command}")
    except (
        EmbeddingUnavailableError,
        BatchWiringError,
        MemoryStoreError,
        ModelCallError,
        OSError,
        ReasoningBankError,
        ServiceError,
        SessionParseError,
        SkillCompilerError,
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
