from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest
import sqlite_vec

from local_memory.dedup import MemoryDeduplicator
from local_memory.distill import MemoryDistiller, parse_memory_response
from local_memory.embedder import EmbeddingBatch
from local_memory.judge import JudgeResponseError, JudgeVerdict, TrajectoryJudge
from local_memory.prompts_reasoning_bank import FAILED_SI, SUCCESSFUL_SI
from local_memory.retrieval import MemoryRetriever
from local_memory.store import MemoryItemType, MemoryStatus, MemoryStore, NewMemory


class SemanticStubEmbedder:
    def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        return EmbeddingBatch(
            model="semantic-stub",
            dimension=3,
            embeddings=[self._vector(text) for text in texts],
        )

    @staticmethod
    def _vector(text: str) -> list[float]:
        lowered = text.lower()
        if "axis-y" in lowered or "far-memory" in lowered:
            return [0.0, 1.0, 0.0]
        if "near-memory" in lowered:
            return [0.98, 0.2, 0.0]
        if "second-memory" in lowered:
            return [0.8, 0.6, 0.0]
        return [1.0, 0.0, 0.0]


def new_memory(
    title: str,
    *,
    status: MemoryStatus = "success",
    session_id: str = "session-1",
    item_type: MemoryItemType = "strategy",
) -> NewMemory:
    assert status in ("success", "fail")
    return NewMemory(
        title=title,
        description=f"Description for {title}",
        content=f"Content for {title}",
        status=status,
        item_type=item_type,
        task_query=f"Task for {title}",
        session_id=session_id,
        template_key="fixture-template",
    )


def open_store(path: Path) -> MemoryStore:
    return MemoryStore(
        path,
        embedder=SemanticStubEmbedder(),
        embedding_dimension=3,
    )


def test_schema_round_trip_and_vector_cleanup(tmp_path: Path) -> None:
    database_path = tmp_path / "nested" / "synth.db"
    with open_store(database_path) as store:
        inserted = store.insert(new_memory("round-trip", status="fail"))
        assert inserted.id > 0
        assert inserted.status == "fail"
        assert store.get(inserted.id) == inserted
        assert store.list() == [inserted]

    connection = sqlite3.connect(database_path)
    connection.enable_load_extension(True)
    sqlite_vec.load(connection)
    connection.enable_load_extension(False)
    assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    assert connection.execute("SELECT count(*) FROM memory_embeddings").fetchone() == (
        1,
    )
    connection.close()

    with open_store(database_path) as reopened:
        assert reopened.get(inserted.id) == inserted
        assert reopened.delete(inserted.id) is True
        assert reopened.delete(inserted.id) is False
        assert reopened.list() == []


def test_embedding_dedup_threshold_skips_near_and_inserts_far(
    tmp_path: Path,
) -> None:
    with open_store(tmp_path / "dedup.db") as store:
        deduplicator = MemoryDeduplicator(store, threshold=0.95)
        original = deduplicator.add(new_memory("base-memory"))
        near = deduplicator.add(new_memory("near-memory"))
        far = deduplicator.add(new_memory("far-memory"))
        framing = deduplicator.add(
            new_memory("near-memory", item_type="meta_heuristic")
        )

        assert original.action == "inserted"
        assert near.action == "skipped"
        assert near.duplicate_id == original.item.id
        assert near.similarity == pytest.approx(0.9798, abs=0.001)
        assert far.action == "inserted"
        assert framing.action == "inserted"
        assert len(store.list()) == 3


def test_dedup_merge_updates_content_without_duplicating_provenance(
    tmp_path: Path,
) -> None:
    with open_store(tmp_path / "merge.db") as store:
        deduplicator = MemoryDeduplicator(store, threshold=0.95, policy="merge")
        first = deduplicator.add(new_memory("base-memory"))
        incoming = new_memory("near-memory", session_id="new-session")
        merged = deduplicator.add(incoming)

        assert merged.action == "merged"
        assert merged.item.id == first.item.id
        assert merged.item.session_id == "session-1"
        assert incoming.content in merged.item.content
        assert len(store.list()) == 1


def test_retrieval_applies_top_k_min_score_and_provenance(tmp_path: Path) -> None:
    with open_store(tmp_path / "retrieval.db") as store:
        store.insert(new_memory("best-memory", status="success"))
        store.insert(new_memory("second-memory", status="fail"))
        store.insert(new_memory("axis-y", status="success"))
        retriever = MemoryRetriever(store, top_k=2, min_score=0.75)

        results = retriever.retrieve("query-best")
        assert [result.item.title for result in results] == [
            "best-memory",
            "second-memory",
        ]
        assert results[0].marked_title == "[success] best-memory"
        assert results[1].provenance.status == "fail"
        assert results[1].score == pytest.approx(0.8, abs=0.001)

        filtered = retriever.retrieve("query-best", top_k=3, min_score=0.9)
        assert [result.item.title for result in filtered] == ["best-memory"]


def test_parser_caps_at_three_and_reports_malformed_items() -> None:
    response = "\n\n".join(
        f"""# Memory Item {index}
## Title Title {index}
## Description Description {index}
## Content Content {index}"""
        for index in range(1, 5)
    )
    capped = parse_memory_response(response)
    assert len(capped.items) == 3
    assert capped.items[2].title == "Title 3"
    assert capped.errors == (
        "response contains 4 items; only the first 3 were accepted",
    )

    malformed = parse_memory_response(
        """# Memory Item 1
## Title A title
## Description A description without content
"""
    )
    assert malformed.items == ()
    assert malformed.errors == ("item 1 is missing content",)


@pytest.mark.parametrize(
    ("verdict", "expected_system_prompt", "expected_status"),
    [
        ("success", SUCCESSFUL_SI, "success"),
        ("failure", FAILED_SI, "fail"),
    ],
)
def test_distillation_uses_success_and_failure_extraction_paths(
    tmp_path: Path,
    verdict: JudgeVerdict,
    expected_system_prompt: str,
    expected_status: MemoryStatus,
) -> None:
    calls: list[tuple[str, str]] = []

    def model_call(system_prompt: str, prompt: str) -> str:
        calls.append((system_prompt, prompt))
        return """# Memory Item 1
## Title Inspect the actual failure boundary
## Description Verify the failing layer before selecting a repair.
## Content Capture the concrete error and reproduce it at the narrowest boundary.
"""

    with open_store(tmp_path / f"{verdict}.db") as store:
        distiller = MemoryDistiller(model_call, MemoryDeduplicator(store))
        result = distiller.distill(
            task_query="Repair a failing local tool",
            trajectory="The agent reproduced the error and inspected its source.",
            verdict=verdict,
            session_id=f"{verdict}-session",
            judge_reasoning="The observed outcome supports this verdict.",
        )

        assert result.inserted_count == 1
        assert result.parse_errors == ()
        assert calls[0][0] == expected_system_prompt
        assert result.outcomes[0].item.status == expected_status


def test_judge_uses_native_priors_as_context_not_verdict() -> None:
    captured_prompt = ""

    def model_call(_system_prompt: str, prompt: str) -> str:
        nonlocal captured_prompt
        captured_prompt = prompt
        return (
            "Thoughts: The trajectory did not satisfy the requested constraint.\n"
            'Status: "failure"'
        )

    result = TrajectoryJudge(model_call).judge(
        task_query="Apply every requested change",
        trajectory="Only one of two requested changes was applied.",
        task_state="completed",
        exit_code=0,
    )
    assert result.verdict == "failure"
    assert "Task state: completed" in captured_prompt
    assert "Exit code: 0" in captured_prompt

    with pytest.raises(JudgeResponseError, match="Thoughts and Status"):
        TrajectoryJudge(lambda _system, _prompt: "Status: success").judge(
            task_query="task", trajectory="trajectory"
        )
