"""Bridge command dispatch with bounded, server-controlled local resources."""

from __future__ import annotations

import platform
from pathlib import Path
from typing import cast

from local_memory.embedder import EmbeddingUnavailableError, LocalEmbedder
from synth import __version__
from synth.protocol import JsonObject, JsonValue

PROTOCOL_VERSION = 1
COMMANDS = ("ping", "embed", "memory.query", "memory.insert")
MAX_EMBED_TEXTS = 32
MAX_EMBED_CHARACTERS = 32_768


class ServiceError(ValueError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _reject_extra(params: JsonObject, allowed: set[str]) -> None:
    unexpected = set(params) - allowed
    if unexpected:
        raise ServiceError(
            "invalid_params",
            f"unexpected parameters: {', '.join(sorted(unexpected))}",
        )


def _string_list(value: JsonValue | None, name: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise ServiceError(
            "invalid_params", f"{name} must be a nonempty array of strings"
        )
    return cast(list[str], value)


def _validate_memory_insert(params: JsonObject) -> None:
    _reject_extra(params, {"item"})
    item = params.get("item")
    if not isinstance(item, dict):
        raise ServiceError("invalid_params", "item must be an object")
    required = {
        "title",
        "description",
        "content",
        "status",
        "itemType",
        "taskQuery",
        "sessionId",
        "templateKey",
    }
    if set(item) != required:
        raise ServiceError(
            "invalid_params", "memory.insert item does not match the Phase 4 contract"
        )
    if not all(isinstance(item[field], str) for field in required):
        raise ServiceError(
            "invalid_params", "memory.insert item fields must be strings"
        )
    if item["status"] not in {"success", "fail"}:
        raise ServiceError("invalid_params", "status must be success or fail")
    if item["itemType"] not in {"strategy", "meta_heuristic"}:
        raise ServiceError(
            "invalid_params", "itemType must be strategy or meta_heuristic"
        )


def _validate_memory_query(params: JsonObject) -> None:
    _reject_extra(
        params,
        {"query", "taskDescription", "k", "minScore", "statuses", "itemTypes"},
    )
    query = params.get("query")
    task = params.get("taskDescription")
    k = params.get("k")
    min_score = params.get("minScore")
    if not isinstance(query, str) or not query:
        raise ServiceError("invalid_params", "query must be a nonempty string")
    if not isinstance(task, str) or not task:
        raise ServiceError(
            "invalid_params", "taskDescription must be a nonempty string"
        )
    if not isinstance(k, int) or isinstance(k, bool) or not 1 <= k <= 20:
        raise ServiceError("invalid_params", "k must be an integer from 1 to 20")
    if (
        not isinstance(min_score, (int, float))
        or isinstance(min_score, bool)
        or not 0 <= float(min_score) <= 1
    ):
        raise ServiceError("invalid_params", "minScore must be between 0 and 1")
    statuses = _string_list(params.get("statuses"), "statuses")
    item_types = _string_list(params.get("itemTypes"), "itemTypes")
    if not set(statuses) <= {"success", "fail"}:
        raise ServiceError("invalid_params", "statuses contains an unknown value")
    if not set(item_types) <= {"strategy", "meta_heuristic"}:
        raise ServiceError("invalid_params", "itemTypes contains an unknown value")


class BridgeService:
    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace.resolve()
        self._embedder: LocalEmbedder | None = None

    def _embedder_or_create(self) -> LocalEmbedder:
        if self._embedder is None:
            self._embedder = LocalEmbedder(self._workspace)
        return self._embedder

    def handle(self, command: str, params: JsonObject) -> JsonObject:
        if command == "ping":
            _reject_extra(params, {"protocolVersion"})
            requested_version = params.get("protocolVersion")
            if requested_version is not None and requested_version != PROTOCOL_VERSION:
                raise ServiceError(
                    "invalid_params",
                    f"protocolVersion must be {PROTOCOL_VERSION}",
                )
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "bridgeVersion": __version__,
                "pythonVersion": platform.python_version(),
                "commands": cast(JsonValue, list(COMMANDS)),
                "memoryAvailable": False,
            }
        if command == "embed":
            _reject_extra(params, {"texts", "kind"})
            texts = _string_list(params.get("texts"), "texts")
            if len(texts) > MAX_EMBED_TEXTS:
                raise ServiceError(
                    "invalid_params",
                    f"texts may contain at most {MAX_EMBED_TEXTS} items",
                )
            if sum(len(text) for text in texts) > MAX_EMBED_CHARACTERS:
                raise ServiceError(
                    "invalid_params",
                    f"texts may contain at most {MAX_EMBED_CHARACTERS} characters",
                )
            kind = params.get("kind")
            if kind not in {"document", "query"}:
                raise ServiceError("invalid_params", "kind must be document or query")
            try:
                batch = self._embedder_or_create().embed(texts)
            except EmbeddingUnavailableError as error:
                raise ServiceError(
                    "embedding_unavailable", str(error), retryable=False
                ) from error
            return {
                "model": batch.model,
                "dimension": batch.dimension,
                "embeddings": cast(JsonValue, batch.embeddings),
            }
        if command == "memory.insert":
            _validate_memory_insert(params)
            raise ServiceError(
                "feature_disabled",
                "ReasoningBank storage is not enabled until Phase 4",
            )
        if command == "memory.query":
            _validate_memory_query(params)
            raise ServiceError(
                "feature_disabled",
                "ReasoningBank retrieval is not enabled until Phase 4",
            )
        raise ServiceError("unknown_command", f"unknown command: {command}")
