"""Embedding-similarity deduplication for distilled memory items."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from local_memory.store import MemoryItem, MemoryStore, NewMemory

DedupPolicy = Literal["skip", "merge"]
DedupAction = Literal["inserted", "skipped", "merged"]
DEFAULT_DEDUP_THRESHOLD = 0.92


@dataclass(frozen=True, slots=True)
class DedupResult:
    action: DedupAction
    item: MemoryItem
    duplicate_id: int | None = None
    similarity: float | None = None


class MemoryDeduplicator:
    """Insert a memory or resolve its nearest duplicate by policy."""

    def __init__(
        self,
        store: MemoryStore,
        *,
        threshold: float = DEFAULT_DEDUP_THRESHOLD,
        policy: DedupPolicy = "skip",
    ) -> None:
        if not -1.0 <= threshold <= 1.0:
            raise ValueError("threshold must be between -1 and 1")
        if policy not in ("skip", "merge"):
            raise ValueError(f"unsupported dedup policy: {policy}")
        self.store = store
        self.threshold = threshold
        self.policy = policy

    def add(self, memory: NewMemory) -> DedupResult:
        candidate_vector = self.store.embed_memory(memory)
        nearest = self.store.search_vector(candidate_vector, top_k=1)
        if not nearest or nearest[0].similarity <= self.threshold:
            inserted = self.store.insert(memory, embedding=candidate_vector)
            return DedupResult(action="inserted", item=inserted)

        duplicate = nearest[0]
        if self.policy == "skip":
            return DedupResult(
                action="skipped",
                item=duplicate.item,
                duplicate_id=duplicate.item.id,
                similarity=duplicate.similarity,
            )

        merged = self.store.update_content(
            duplicate.item.id,
            description=_merge_text(
                duplicate.item.description, memory.description, separator=" "
            ),
            content=_merge_text(
                duplicate.item.content, memory.content, separator="\n\n"
            ),
        )
        return DedupResult(
            action="merged",
            item=merged,
            duplicate_id=duplicate.item.id,
            similarity=duplicate.similarity,
        )


def _merge_text(existing: str, incoming: str, *, separator: str) -> str:
    normalized_existing = existing.strip()
    normalized_incoming = incoming.strip()
    if not normalized_incoming or normalized_incoming in normalized_existing:
        return normalized_existing
    if normalized_existing in normalized_incoming:
        return normalized_incoming
    return f"{normalized_existing}{separator}{normalized_incoming}"
