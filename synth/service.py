"""Bridge command dispatch with bounded, server-controlled local resources."""

from __future__ import annotations

import platform
import sqlite3
from pathlib import Path
from typing import cast

from local_memory.embedder import EmbeddingUnavailableError, LocalEmbedder
from local_memory.retrieval import MemoryRetriever
from local_memory.store import (
    DEFAULT_BUSY_TIMEOUT_MS,
    MemoryItem,
    MemoryItemType,
    MemoryStatus,
    MemoryStore,
    MemoryStoreError,
    NewMemory,
)
from synth import __version__
from synth.config import SynthSettings, load_synth_settings
from synth.protocol import JsonObject, JsonValue

PROTOCOL_VERSION = 1
COMMANDS = ("ping", "embed", "memory.query", "memory.insert", "memory.list")
MAX_EMBED_TEXTS = 32
MAX_EMBED_CHARACTERS = 32_768
MAX_MEMORY_LIST_ITEMS = 100


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


def _validate_memory_insert(params: JsonObject) -> JsonObject:
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
    string_fields = required - {"templateKey"}
    if not all(isinstance(item[field], str) for field in string_fields):
        raise ServiceError(
            "invalid_params", "memory.insert item fields must be strings"
        )
    if item["templateKey"] is not None and not isinstance(item["templateKey"], str):
        raise ServiceError(
            "invalid_params", "memory.insert templateKey must be a string or null"
        )
    if item["status"] not in {"success", "fail"}:
        raise ServiceError("invalid_params", "status must be success or fail")
    if item["itemType"] not in {"strategy", "meta_heuristic"}:
        raise ServiceError(
            "invalid_params", "itemType must be strategy or meta_heuristic"
        )
    return item


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


def _validate_memory_list(params: JsonObject) -> int:
    _reject_extra(params, {"limit"})
    limit = params.get("limit", 50)
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= MAX_MEMORY_LIST_ITEMS
    ):
        raise ServiceError(
            "invalid_params",
            f"limit must be an integer from 1 to {MAX_MEMORY_LIST_ITEMS}",
        )
    return limit


def _memory_item(item: MemoryItem, *, score: float | None = None) -> JsonObject:
    return {
        "id": item.id,
        "title": item.title,
        "description": item.description,
        "content": item.content,
        "status": item.status,
        "itemType": item.item_type,
        "taskQuery": item.task_query,
        "sessionId": item.session_id,
        "templateKey": item.template_key,
        "successCount": item.success_count,
        "failureCount": item.failure_count,
        "confidence": item.confidence,
        "createdAt": item.created_at,
        **({"score": score} if score is not None else {}),
    }


class BridgeService:
    def __init__(
        self,
        workspace: Path,
        *,
        memory_busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        self._workspace = workspace.resolve()
        self._settings: SynthSettings = load_synth_settings(self._workspace)
        self._memory_busy_timeout_ms = memory_busy_timeout_ms
        self._embedder: LocalEmbedder | None = None
        self._memory_store: MemoryStore | None = None

    def _embedder_or_create(self) -> LocalEmbedder:
        if self._embedder is None:
            self._embedder = LocalEmbedder(
                self._workspace,
                model_name=self._settings.memory.embed_model,
            )
        return self._embedder

    def _memory_store_or_create(self) -> MemoryStore:
        if (
            self._settings.kill_switch
            or not self._settings.memory.enabled
        ):
            raise ServiceError(
                "feature_disabled", "ReasoningBank memory is disabled in synth.json"
            )
        if self._memory_store is None:
            self._memory_store = MemoryStore(
                self._settings.memory.database_path,
                embedder=self._embedder_or_create(),
                busy_timeout_ms=self._memory_busy_timeout_ms,
                workspace=self._workspace,
            )
        return self._memory_store

    def close(self) -> None:
        if self._memory_store is not None:
            self._memory_store.close()
            self._memory_store = None

    def _memory_error(self, error: Exception) -> ServiceError:
        if isinstance(error, ServiceError):
            return error
        if isinstance(error, EmbeddingUnavailableError):
            return ServiceError("embedding_unavailable", str(error))
        if isinstance(error, sqlite3.OperationalError) and any(
            marker in str(error).lower() for marker in ("locked", "busy")
        ):
            return ServiceError(
                "memory_busy",
                "ReasoningBank database is busy",
                retryable=True,
            )
        if isinstance(error, (MemoryStoreError, sqlite3.Error, OSError, ValueError)):
            return ServiceError("memory_unavailable", str(error))
        return ServiceError("memory_unavailable", "ReasoningBank operation failed")

    def _handle_memory(self, command: str, params: JsonObject) -> JsonObject:
        store = self._memory_store_or_create()
        if command == "memory.list":
            limit = _validate_memory_list(params)
            items = [_memory_item(item) for item in store.list(limit=limit)]
            return {"items": cast(JsonValue, items)}
        if command == "memory.insert":
            item = _validate_memory_insert(params)
            template_key = item["templateKey"]
            inserted = store.insert(
                NewMemory(
                    title=cast(str, item["title"]),
                    description=cast(str, item["description"]),
                    content=cast(str, item["content"]),
                    status=cast(MemoryStatus, item["status"]),
                    item_type=cast(MemoryItemType, item["itemType"]),
                    task_query=cast(str, item["taskQuery"]),
                    session_id=cast(str, item["sessionId"]),
                    template_key=(cast(str, template_key) if template_key else None),
                )
            )
            return {"item": _memory_item(inserted)}
        if command == "memory.query":
            _validate_memory_query(params)
            query = cast(str, params["query"])
            task_description = cast(str, params["taskDescription"])
            top_k = cast(int, params["k"])
            min_score = float(cast(int | float, params["minScore"]))
            statuses = set(cast(list[str], params["statuses"]))
            item_types = set(cast(list[str], params["itemTypes"]))
            stored_count = store.count()
            if stored_count == 0:
                return {"items": []}
            results = MemoryRetriever(
                store,
                top_k=max(top_k, stored_count),
                min_score=min_score,
                task_description=task_description,
            ).retrieve(query)
            eligible = [
                result
                for result in results
                if result.item.status in statuses
                and result.item.item_type in item_types
            ][:top_k]
            items = [
                _memory_item(result.item, score=result.score) for result in eligible
            ]
            return {"items": cast(JsonValue, items)}
        raise ServiceError("unknown_command", f"unknown command: {command}")

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
                "memoryAvailable": (
                    self._settings.memory.enabled and not self._settings.kill_switch
                ),
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
        if command.startswith("memory."):
            try:
                return self._handle_memory(command, params)
            except ServiceError:
                raise
            except Exception as error:
                raise self._memory_error(error) from error
        raise ServiceError("unknown_command", f"unknown command: {command}")
