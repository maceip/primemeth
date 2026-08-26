"""Long-lived Synth JSON-lines bridge process."""

from __future__ import annotations

import sys
from pathlib import Path

from synth.protocol import (
    JsonObject,
    ProtocolError,
    encode_message,
    error_response,
    parse_request,
    success_response,
)
from synth.service import BridgeService, ServiceError

MAX_REQUEST_BYTES = 1_048_576


def _write_response(response: JsonObject) -> None:
    sys.stdout.write(f"{encode_message(response)}\n")
    sys.stdout.flush()


def run_bridge(workspace: Path | None = None) -> int:
    service = BridgeService(workspace or Path.cwd())
    for raw_line in sys.stdin.buffer:
        if len(raw_line) > MAX_REQUEST_BYTES:
            _write_response(
                error_response(
                    ProtocolError(
                        "invalid_request",
                        f"request exceeds {MAX_REQUEST_BYTES} bytes",
                    )
                )
            )
            continue
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError:
            _write_response(
                error_response(
                    ProtocolError("invalid_json", "request is not valid UTF-8")
                )
            )
            continue
        request_id: str | None = None
        try:
            request = parse_request(line)
            request_id = request.request_id
            result = service.handle(request.command, request.params)
            response = success_response(request.request_id, result)
        except ProtocolError as error:
            response = error_response(error)
        except ServiceError as error:
            response = error_response(
                ProtocolError(
                    error.code,
                    str(error),
                    request_id=request_id,
                    retryable=error.retryable,
                )
            )
        except Exception:
            response = error_response(
                ProtocolError(
                    "internal_error",
                    "bridge request failed",
                    request_id=request_id,
                )
            )
        try:
            _write_response(response)
        except BrokenPipeError:
            return 1
    return 0
