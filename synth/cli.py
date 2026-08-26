"""The single Python product entry point for Synth."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from middleware.telemetry import TelemetryValidationError, validate_telemetry_file
from synth import __version__
from synth.bridge import run_bridge
from synth.capabilities import CAPABILITIES
from synth.ports import prompt_port_manifest
from synth.protocol import JsonObject, JsonValue
from synth.service import PROTOCOL_VERSION, BridgeService, ServiceError


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


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
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
            _write_json(
                _service().handle("ping", {"protocolVersion": PROTOCOL_VERSION})
            )
            return 0
        if command == "embed":
            texts = cast(list[str], args.text)
            kind = cast(str, args.kind)
            params: JsonObject = {
                "texts": cast(JsonValue, texts),
                "kind": kind,
            }
            _write_json(_service().handle("embed", params))
            return 0
        if command == "ports":
            _write_json(prompt_port_manifest())
            return 0
        if command == "telemetry":
            telemetry_command = cast(str, args.telemetry_command)
            if telemetry_command != "validate":
                raise ValueError(f"unknown telemetry command: {telemetry_command}")
            path = cast(Path, args.path)
            result = validate_telemetry_file(path)
            _write_json({"events": result.events, "sessions": result.sessions})
            return 0
        if command == "bridge":
            return run_bridge()
        raise ValueError(f"unknown command: {command}")
    except (OSError, ServiceError, TelemetryValidationError, ValueError) as error:
        print(f"synth: {error}", file=sys.stderr)
        return 2


def bridge_main() -> int:
    return run_bridge()


if __name__ == "__main__":
    raise SystemExit(main())
