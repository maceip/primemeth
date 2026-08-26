"""Strict JSON-lines framing shared by the Synth CLI and bridge."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)
type JsonObject = dict[str, JsonValue]


class ProtocolError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        request_id: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class BridgeRequest:
    request_id: str
    command: str
    params: JsonObject


def is_json_value(value: object) -> bool:
    if value is None or isinstance(value, (bool, int, str)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and is_json_value(item) for key, item in value.items()
        )
    return False


def parse_request(line: str) -> BridgeRequest:
    try:
        value: object = json.loads(line)
    except json.JSONDecodeError as error:
        raise ProtocolError("invalid_json", "request is not valid JSON") from error

    request_id: str | None = None
    if isinstance(value, dict) and isinstance(value.get("id"), str):
        request_id = value["id"]
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ProtocolError(
            "invalid_request", "request must be an object", request_id=request_id
        )
    unexpected = set(value) - {"id", "command", "params"}
    if unexpected:
        raise ProtocolError(
            "invalid_request",
            f"unexpected request fields: {', '.join(sorted(unexpected))}",
            request_id=request_id,
        )
    raw_id = value.get("id")
    raw_command = value.get("command")
    raw_params = value.get("params")
    if not isinstance(raw_id, str) or not raw_id or len(raw_id) > 128:
        raise ProtocolError(
            "invalid_request",
            "id must be a nonempty string of at most 128 characters",
            request_id=request_id,
        )
    if not isinstance(raw_command, str) or not raw_command or len(raw_command) > 64:
        raise ProtocolError(
            "invalid_request",
            "command must be a nonempty string of at most 64 characters",
            request_id=raw_id,
        )
    if not isinstance(raw_params, dict) or not is_json_value(raw_params):
        raise ProtocolError(
            "invalid_request", "params must be a JSON object", request_id=raw_id
        )
    return BridgeRequest(
        request_id=raw_id,
        command=raw_command,
        params=raw_params,
    )


def success_response(request_id: str, result: JsonObject) -> JsonObject:
    return {"id": request_id, "ok": True, "result": result}


def error_response(error: ProtocolError) -> JsonObject:
    return {
        "id": error.request_id,
        "ok": False,
        "error": {
            "code": error.code,
            "message": str(error),
            "retryable": error.retryable,
        },
    }


def encode_message(message: JsonObject) -> str:
    if not is_json_value(message):
        raise TypeError("bridge response is not JSON-compatible")
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))
