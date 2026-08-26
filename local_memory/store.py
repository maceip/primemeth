"""ReasoningBank persistence.

Wired into synth/service.py at Phase 4 integration (orphan-check exemption).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Literal, Protocol, cast

import sqlite_vec  # type: ignore[import-untyped]
from sqlite_vec import serialize_float32

from local_memory.embedder import (
    DEFAULT_EMBED_DIMENSION,
    EmbeddingBatch,
    LocalEmbedder,
)

MemoryStatus = Literal["success", "fail"]
MemoryItemType = Literal["strategy", "meta_heuristic"]

DEFAULT_DATABASE_PATH = Path(".synth/synth.db")
DEFAULT_BUSY_TIMEOUT_MS = 5_000
_SCHEMA_DEFAULT_VECTOR = f"FLOAT[{DEFAULT_EMBED_DIMENSION}]"
_VALID_STATUSES = frozenset({"success", "fail"})
_VALID_ITEM_TYPES = frozenset({"strategy", "meta_heuristic"})


class Embedder(Protocol):
    """Minimal interface shared by LocalEmbedder and deterministic test embedders."""

    def embed(self, texts: Sequence[str]) -> EmbeddingBatch: ...


class MemoryStoreError(RuntimeError):
    """Base error for persistent memory operations."""


class MemorySchemaError(MemoryStoreError):
    """Raised when an existing vector table has an incompatible dimension."""


class EmbeddingDimensionError(MemoryStoreError):
    """Raised when an embedder returns a vector with an unexpected dimension."""


@dataclass(frozen=True, slots=True)
class NewMemory:
    title: str
    description: str
    content: str
    status: MemoryStatus
    item_type: MemoryItemType
    task_query: str
    session_id: str
    template_key: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryItem:
    id: int
    title: str
    description: str
    content: str
    status: MemoryStatus
    item_type: MemoryItemType
    task_query: str
    session_id: str
    template_key: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class VectorMatch:
    item: MemoryItem
    similarity: float


def memory_embedding_text(memory: NewMemory | MemoryItem) -> str:
    """Build the document text persisted in the vector index."""

    return (
        f"Task: {memory.task_query}\n"
        f"Title: {memory.title}\n"
        f"Description: {memory.description}\n"
        f"Content: {memory.content}"
    )


class MemoryStore:
    """SQLite metadata and sqlite-vec embeddings for distilled memory items."""

    def __init__(
        self,
        database_path: str | Path = DEFAULT_DATABASE_PATH,
        *,
        embedder: Embedder | None = None,
        embedding_dimension: int = DEFAULT_EMBED_DIMENSION,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        workspace: Path | None = None,
    ) -> None:
        if embedding_dimension <= 0:
            raise ValueError("embedding_dimension must be positive")
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms cannot be negative")

        database_name = str(database_path)
        self.database_path: str | Path
        if database_name == ":memory:":
            self.database_path = database_name
        else:
            self.database_path = Path(database_path).expanduser()
        self.embedding_dimension = embedding_dimension
        resolved_workspace = (workspace or Path.cwd()).resolve()
        self.embedder = embedder or LocalEmbedder(resolved_workspace)

        if database_name != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(self.database_path),
            timeout=busy_timeout_ms / 1_000,
        )
        self._connection.row_factory = sqlite3.Row
        try:
            self._load_vector_extension()
            self._configure(busy_timeout_ms)
            self._initialize_schema()
        except Exception:
            self._connection.close()
            raise

    def __enter__(self) -> MemoryStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def _load_vector_extension(self) -> None:
        try:
            self._connection.enable_load_extension(True)
            sqlite_vec.load(self._connection)
        finally:
            self._connection.enable_load_extension(False)

    def _configure(self, busy_timeout_ms: int) -> None:
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        if str(self.database_path) != ":memory:":
            mode = self._connection.execute("PRAGMA journal_mode = WAL").fetchone()
            if mode is None or str(mode[0]).lower() != "wal":
                raise MemoryStoreError("SQLite refused WAL journal mode")

    def _initialize_schema(self) -> None:
        schema_path = Path(__file__).with_name("schema.sql")
        schema = schema_path.read_text()
        if schema.count(_SCHEMA_DEFAULT_VECTOR) != 1:
            raise MemorySchemaError(
                "schema.sql must contain exactly one default vector dimension"
            )
        rendered = schema.replace(
            _SCHEMA_DEFAULT_VECTOR, f"FLOAT[{self.embedding_dimension}]"
        )
        self._connection.executescript(rendered)
        self._validate_existing_dimension()

    def _validate_existing_dimension(self) -> None:
        row = self._connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'memory_embeddings'"
        ).fetchone()
        if row is None:
            raise MemorySchemaError("memory_embeddings was not created")
        match = re.search(r"embedding\s+FLOAT\[(\d+)\]", str(row[0]), re.IGNORECASE)
        if match is None:
            raise MemorySchemaError("cannot determine memory embedding dimension")
        actual_dimension = int(match.group(1))
        if actual_dimension != self.embedding_dimension:
            raise MemorySchemaError(
                "existing memory embedding dimension "
                f"{actual_dimension} does not match configured dimension "
                f"{self.embedding_dimension}"
            )

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        batch = self.embedder.embed(texts)
        if batch.dimension != self.embedding_dimension:
            raise EmbeddingDimensionError(
                f"embedder returned dimension {batch.dimension}; "
                f"store requires {self.embedding_dimension}"
            )
        if len(batch.embeddings) != len(texts):
            raise MemoryStoreError(
                "embedder returned an unexpected number of embeddings"
            )
        for vector in batch.embeddings:
            self._validate_vector(vector)
        return batch.embeddings

    def embed_memory(self, memory: NewMemory | MemoryItem) -> list[float]:
        return self.embed_texts([memory_embedding_text(memory)])[0]

    def insert(
        self,
        memory: NewMemory,
        *,
        embedding: Sequence[float] | None = None,
    ) -> MemoryItem:
        normalized = _validated_memory(memory)
        vector = (
            list(embedding) if embedding is not None else self.embed_memory(normalized)
        )
        self._validate_vector(vector)
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO memory_items (
                    title, description, content, status, item_type,
                    task_query, session_id, template_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized.title,
                    normalized.description,
                    normalized.content,
                    normalized.status,
                    normalized.item_type,
                    normalized.task_query,
                    normalized.session_id,
                    normalized.template_key,
                ),
            )
            memory_id = cursor.lastrowid
            if memory_id is None:
                raise MemoryStoreError("SQLite did not return an inserted memory id")
            self._connection.execute(
                "INSERT INTO memory_embeddings (memory_id, embedding) VALUES (?, ?)",
                (memory_id, serialize_float32(vector)),
            )
        item = self.get(memory_id)
        if item is None:
            raise MemoryStoreError("inserted memory item could not be read back")
        return item

    def get(self, memory_id: int) -> MemoryItem | None:
        row = self._connection.execute(
            "SELECT * FROM memory_items WHERE id = ?", (memory_id,)
        ).fetchone()
        return _row_to_item(row) if row is not None else None

    def list(self, *, limit: int | None = None) -> list[MemoryItem]:
        if limit is not None and limit < 0:
            raise ValueError("limit cannot be negative")
        sql = "SELECT * FROM memory_items ORDER BY created_at, id"
        parameters: tuple[int, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            parameters = (limit,)
        rows = self._connection.execute(sql, parameters).fetchall()
        return [_row_to_item(row) for row in rows]

    def delete(self, memory_id: int) -> bool:
        with self._connection:
            self._connection.execute(
                "DELETE FROM memory_embeddings WHERE memory_id = ?", (memory_id,)
            )
            cursor = self._connection.execute(
                "DELETE FROM memory_items WHERE id = ?", (memory_id,)
            )
        return cursor.rowcount > 0

    def update_content(
        self,
        memory_id: int,
        *,
        description: str,
        content: str,
    ) -> MemoryItem:
        existing = self.get(memory_id)
        if existing is None:
            raise KeyError(f"memory item {memory_id} does not exist")
        merged = NewMemory(
            title=existing.title,
            description=description,
            content=content,
            status=existing.status,
            item_type=existing.item_type,
            task_query=existing.task_query,
            session_id=existing.session_id,
            template_key=existing.template_key,
        )
        normalized = _validated_memory(merged)
        vector = self.embed_memory(normalized)
        with self._connection:
            self._connection.execute(
                "UPDATE memory_items SET description = ?, content = ? WHERE id = ?",
                (normalized.description, normalized.content, memory_id),
            )
            self._connection.execute(
                "UPDATE memory_embeddings SET embedding = ? WHERE memory_id = ?",
                (serialize_float32(vector), memory_id),
            )
        updated = self.get(memory_id)
        if updated is None:
            raise MemoryStoreError("updated memory item could not be read back")
        return updated

    def search_vector(
        self, embedding: Sequence[float], *, top_k: int
    ) -> Sequence[VectorMatch]:
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        vector = list(embedding)
        self._validate_vector(vector)
        rows = self._connection.execute(
            """
            SELECT memory_id, distance
            FROM memory_embeddings
            WHERE embedding MATCH ? AND k = ?
            ORDER BY distance
            """,
            (serialize_float32(vector), top_k),
        ).fetchall()
        rows.sort(key=lambda row: (float(row["distance"]), int(row["memory_id"])))
        matches: list[VectorMatch] = []
        for row in rows:
            item = self.get(int(row["memory_id"]))
            if item is None:
                raise MemoryStoreError(
                    f"embedding {row['memory_id']} has no memory item"
                )
            matches.append(
                VectorMatch(item=item, similarity=1.0 - float(row["distance"]))
            )
        return matches

    def _validate_vector(self, vector: Sequence[float]) -> None:
        if len(vector) != self.embedding_dimension:
            raise EmbeddingDimensionError(
                f"embedding has dimension {len(vector)}; "
                f"store requires {self.embedding_dimension}"
            )
        if not all(isfinite(component) for component in vector):
            raise EmbeddingDimensionError("embedding contains a non-finite component")


def _validated_memory(memory: NewMemory) -> NewMemory:
    title = memory.title.strip()
    description = memory.description.strip()
    content = memory.content.strip()
    task_query = memory.task_query.strip()
    session_id = memory.session_id.strip()
    if not all((title, description, content, task_query, session_id)):
        raise ValueError("memory text and provenance fields cannot be empty")
    if memory.status not in _VALID_STATUSES:
        raise ValueError(f"invalid memory status: {memory.status}")
    if memory.item_type not in _VALID_ITEM_TYPES:
        raise ValueError(f"invalid memory item type: {memory.item_type}")
    template_key = memory.template_key.strip() if memory.template_key else None
    return NewMemory(
        title=title,
        description=description,
        content=content,
        status=memory.status,
        item_type=memory.item_type,
        task_query=task_query,
        session_id=session_id,
        template_key=template_key,
    )


def _row_to_item(row: sqlite3.Row) -> MemoryItem:
    return MemoryItem(
        id=int(row["id"]),
        title=str(row["title"]),
        description=str(row["description"]),
        content=str(row["content"]),
        status=cast(MemoryStatus, row["status"]),
        item_type=cast(MemoryItemType, row["item_type"]),
        task_query=str(row["task_query"]),
        session_id=str(row["session_id"]),
        template_key=(str(row["template_key"]) if row["template_key"] else None),
        created_at=str(row["created_at"]),
    )
