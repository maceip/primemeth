"""Instruction-aware cosine retrieval for persisted ReasoningBank items."""

from __future__ import annotations

from dataclasses import dataclass

from local_memory.prompts_reasoning_bank import INSTRUCTION_AWARE_QUERY_FORMAT
from local_memory.store import (
    MINIMUM_MEMORY_CONFIDENCE,
    MemoryItem,
    MemoryStatus,
    MemoryStore,
)

DEFAULT_TOP_K = 3
DEFAULT_MIN_SCORE = 0.0
DEFAULT_RETRIEVAL_TASK = (
    "Given prior software engineering memories, analyze the current task's intent "
    "and select relevant strategies or lessons that could help resolve it."
)


@dataclass(frozen=True, slots=True)
class MemoryProvenance:
    status: MemoryStatus
    task_query: str
    session_id: str
    template_key: str | None


@dataclass(frozen=True, slots=True)
class RetrievedMemory:
    item: MemoryItem
    score: float
    provenance: MemoryProvenance

    @property
    def marked_title(self) -> str:
        return f"[{self.provenance.status}] {self.item.title}"


def get_detailed_instruct(task_description: str, query: str) -> str:
    """Port the ReasoningBank query format without binding an embedding backend."""

    if not task_description.strip():
        raise ValueError("task_description cannot be empty")
    if not query.strip():
        raise ValueError("query cannot be empty")
    return INSTRUCTION_AWARE_QUERY_FORMAT.format(
        task_description=task_description.strip(), query=query.strip()
    )


class MemoryRetriever:
    def __init__(
        self,
        store: MemoryStore,
        *,
        top_k: int = DEFAULT_TOP_K,
        min_score: float = DEFAULT_MIN_SCORE,
        task_description: str = DEFAULT_RETRIEVAL_TASK,
    ) -> None:
        _validate_retrieval_options(top_k, min_score)
        self.store = store
        self.top_k = top_k
        self.min_score = min_score
        self.task_description = task_description

    def retrieve(
        self,
        query: str,
        *,
        top_k: int | None = None,
        min_score: float | None = None,
    ) -> list[RetrievedMemory]:
        selected_top_k = self.top_k if top_k is None else top_k
        selected_min_score = self.min_score if min_score is None else min_score
        _validate_retrieval_options(selected_top_k, selected_min_score)
        detailed_query = get_detailed_instruct(self.task_description, query)
        query_vector = self.store.embed_texts([detailed_query])[0]
        stored_count = self.store.count()
        if stored_count == 0:
            return []
        matches = self.store.search_vector(query_vector, top_k=stored_count)
        eligible = [
            RetrievedMemory(
                item=match.item,
                score=match.similarity,
                provenance=MemoryProvenance(
                    status=match.item.status,
                    task_query=match.item.task_query,
                    session_id=match.item.session_id,
                    template_key=match.item.template_key,
                ),
            )
            for match in matches
            if match.similarity >= selected_min_score
        ]
        eligible.sort(
            key=lambda result: (
                -result.score,
                -max(result.item.confidence, MINIMUM_MEMORY_CONFIDENCE),
                result.item.id,
            )
        )
        return eligible[:selected_top_k]


def _validate_retrieval_options(top_k: int, min_score: float) -> None:
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    if not -1.0 <= min_score <= 1.0:
        raise ValueError("min_score must be between -1 and 1")
