"""Runnable fixture proof for the ReasoningBank store and retrieval path."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from tempfile import TemporaryDirectory

from local_memory.dedup import MemoryDeduplicator
from local_memory.distill import MemoryDistiller
from local_memory.embedder import EmbeddingBatch
from local_memory.prompts_reasoning_bank import FAILED_SI
from local_memory.retrieval import MemoryRetriever
from local_memory.store import MemoryStore

_DEMO_DIMENSION = 32
_FAILURE_TRAJECTORY = """1. Tried to open the SQLite database.
2. Created the vector table before loading the sqlite-vec extension.
3. SQLite rejected vec0 with 'no such module', and the session exited without retrying.
"""


class DeterministicFixtureEmbedder:
    """Small token-hash embedder used only to keep the demo offline."""

    def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        return EmbeddingBatch(
            model="fixture-token-hash-v1",
            dimension=_DEMO_DIMENSION,
            embeddings=[_embed_text(text) for text in texts],
        )


def fixture_model(system_prompt: str, prompt: str) -> str:
    if system_prompt != FAILED_SI or "no such module" not in prompt:
        raise ValueError("fixture received an unexpected extraction prompt")
    return (
        "# Memory Item 1\n"
        "## Title Load SQLite extensions before creating virtual tables\n"
        "## Description Initialize sqlite-vec on every connection before applying "
        "vec0 schema.\n"
        "## Content Enable extension loading, load sqlite-vec, disable extension "
        "loading, and only then create or query vector tables.\n"
    )


def main() -> None:
    with (
        TemporaryDirectory(prefix="synth-memory-demo-") as directory,
        MemoryStore(
            f"{directory}/synth.db",
            embedder=DeterministicFixtureEmbedder(),
            embedding_dimension=_DEMO_DIMENSION,
        ) as store,
    ):
        distiller = MemoryDistiller(
            fixture_model,
            MemoryDeduplicator(store, threshold=0.95),
        )
        distilled = distiller.distill(
            task_query="Fix a SQLite vector store that reports no such module vec0",
            trajectory=_FAILURE_TRAJECTORY,
            verdict="failure",
            session_id="fixture-failure-session",
            template_key="sqlite-extension-load",
            judge_reasoning=("The extension was never loaded before schema creation."),
        )
        retrieved = MemoryRetriever(store, min_score=0.15).retrieve(
            "Repair SQLite vector extension loading before creating a vec0 table"
        )
        print(
            f"distilled={distilled.inserted_count} "
            f"parse_errors={len(distilled.parse_errors)}"
        )
        print(f"retrieved={len(retrieved)}")
        for result in retrieved:
            print(f"{result.marked_title} score={result.score:.3f}")


def _embed_text(text: str) -> list[float]:
    vector = [0.0] * _DEMO_DIMENSION
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        digest = hashlib.blake2b(token.encode(), digest_size=4).digest()
        vector[int.from_bytes(digest) % _DEMO_DIMENSION] += 1.0
    norm = math.sqrt(sum(component * component for component in vector))
    if norm == 0:
        vector[0] = 1.0
        return vector
    return [component / norm for component in vector]


if __name__ == "__main__":
    main()
