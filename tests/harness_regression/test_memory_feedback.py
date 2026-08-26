from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

import synth.reasoning_bank as reasoning_bank
from local_memory.embedder import EmbeddingBatch
from local_memory.prompts_reasoning_bank import (
    AUTOEVAL_TRAJECTORY_JUDGE_SYSTEM_PROMPT,
    SUCCESSFUL_SI,
)
from local_memory.retrieval import MemoryRetriever
from local_memory.store import MemoryStore, NewMemory
from middleware.terminal_outcome import (
    OutcomeEvidence,
    TerminalOutcome,
    TerminationReason,
)
from synth.reasoning_bank import distill_session
from synth.session import load_session_trajectory


class ConstantEmbedder:
    def __init__(
        self,
        _workspace: Path | None = None,
        *,
        model_name: str = "memory-feedback-fixture",
    ) -> None:
        self.model_name = model_name

    def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        vector = [1.0, *([0.0] * 383)]
        return EmbeddingBatch(
            model=self.model_name,
            dimension=384,
            embeddings=[vector.copy() for _text in texts],
        )


def test_retrieved_strategy_outcomes_update_confidence_and_ranking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(tmp_path)
    database = tmp_path / ".synth" / "synth.db"
    with MemoryStore(
        database, embedder=ConstantEmbedder(), workspace=tmp_path
    ) as store:
        failed = store.insert(_memory("Failed-session strategy", "seed-failed"))
        succeeded = store.insert(
            _memory("Succeeded-session strategy", "seed-succeeded")
        )
        framing = store.insert(
            _memory(
                "Problem framing",
                "seed-framing",
                item_type="meta_heuristic",
            )
        )

    failed_session = _write_session(
        tmp_path,
        session_id="retrieval-failed",
        task="A deterministic acceptance failure despite an optimistic judge",
        item_ids=[failed.id],
        framing_item_ids=[framing.id],
        abandoned_item_id=99_999,
    )
    succeeded_session = _write_session(
        tmp_path,
        session_id="retrieval-succeeded",
        task="A deterministic acceptance success",
        item_ids=[succeeded.id],
        framing_item_ids=[],
    )
    _write_terminal_outcome(tmp_path, "retrieval-failed", reward=0)
    _write_terminal_outcome(tmp_path, "retrieval-succeeded", reward=1)

    parsed = load_session_trajectory(failed_session)
    assert parsed.retrieved_memory_ids == (failed.id,)

    def model_call(system_prompt: str, _prompt: str) -> str:
        if system_prompt == AUTOEVAL_TRAJECTORY_JUDGE_SYSTEM_PROMPT:
            return (
                "Thoughts: The assistant response claims the task was completed.\n"
                'Status: "success"'
            )
        if system_prompt == SUCCESSFUL_SI:
            return (
                "# Memory Item 1\n"
                "## Title Verify the acceptance boundary\n"
                "## Description Use executable acceptance before concluding.\n"
                "## Content Run the focused check and preserve its result "
                "as evidence.\n"
            )
        raise AssertionError(f"unexpected model prompt: {system_prompt[:80]}")

    monkeypatch.setattr(
        reasoning_bank,
        "configured_model_callable",
        lambda _settings, _fixture_path: model_call,
    )
    monkeypatch.setattr(reasoning_bank, "LocalEmbedder", ConstantEmbedder)

    failed_result = distill_session(failed_session)
    replay_result = distill_session(failed_session)
    succeeded_result = distill_session(succeeded_session)

    assert failed_result["memoryFeedback"] == {
        "outcome": "failure",
        "source": "terminal",
        "updatedItemIds": [failed.id],
        "unchangedItemIds": [],
        "missingItemIds": [],
        "ineligibleItemIds": [],
    }
    assert replay_result["memoryFeedback"] == {
        "outcome": "failure",
        "source": "terminal",
        "updatedItemIds": [],
        "unchangedItemIds": [failed.id],
        "missingItemIds": [],
        "ineligibleItemIds": [],
    }
    assert succeeded_result["memoryFeedback"]["outcome"] == "success"
    assert succeeded_result["memoryFeedback"]["source"] == "terminal"

    with MemoryStore(
        database, embedder=ConstantEmbedder(), workspace=tmp_path
    ) as store:
        failed_after = store.get(failed.id)
        succeeded_after = store.get(succeeded.id)
        framing_after = store.get(framing.id)
        assert failed_after is not None
        assert succeeded_after is not None
        assert framing_after is not None
        assert (failed_after.success_count, failed_after.failure_count) == (0, 1)
        assert (succeeded_after.success_count, succeeded_after.failure_count) == (
            1,
            0,
        )
        assert (framing_after.success_count, framing_after.failure_count) == (0, 0)
        assert failed_after.confidence == pytest.approx(1 / 3)
        assert succeeded_after.confidence == pytest.approx(2 / 3)
        ranked = MemoryRetriever(store, top_k=store.count()).retrieve(
            "A deterministic acceptance task"
        )
        ranked_ids = [result.item.id for result in ranked]
        assert ranked_ids.index(succeeded.id) < ranked_ids.index(failed.id)

    fallback_session = _write_session(
        tmp_path,
        session_id="retrieval-judge-fallback",
        task="A judged success without deterministic acceptance",
        item_ids=[failed.id],
        framing_item_ids=[],
    )
    fallback_result = distill_session(fallback_session)
    assert fallback_result["memoryFeedback"]["outcome"] == "success"
    assert fallback_result["memoryFeedback"]["source"] == "judge"
    with MemoryStore(
        database, embedder=ConstantEmbedder(), workspace=tmp_path
    ) as store:
        failed_after_fallback = store.get(failed.id)
        assert failed_after_fallback is not None
        assert (
            failed_after_fallback.success_count,
            failed_after_fallback.failure_count,
        ) == (1, 1)


def _memory(
    title: str,
    session_id: str,
    *,
    item_type: str = "strategy",
) -> NewMemory:
    assert item_type in {"strategy", "meta_heuristic"}
    return NewMemory(
        title=title,
        description=f"Description for {title}",
        content=f"Content for {title}",
        status="success",
        item_type=item_type,
        task_query="A deterministic acceptance task",
        session_id=session_id,
    )


def _write_config(workspace: Path) -> None:
    (workspace / "synth.json").write_text(
        json.dumps(
            {
                "memory": {
                    "enabled": True,
                    "k": 3,
                    "minScore": 0,
                    "db": ".synth/synth.db",
                    "embedModel": "memory-feedback-fixture",
                    "judgeModel": "small",
                }
            }
        )
    )


def _write_session(
    workspace: Path,
    *,
    session_id: str,
    task: str,
    item_ids: list[int],
    framing_item_ids: list[int],
    abandoned_item_id: int | None = None,
) -> Path:
    records: list[dict[str, object]] = [
        {
            "type": "session",
            "version": 3,
            "id": session_id,
            "cwd": str(workspace),
            "parentSession": None,
        },
        {
            "type": "message",
            "id": "user",
            "parentId": None,
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": task}],
            },
        },
    ]
    if abandoned_item_id is not None:
        records.append(
            {
                "type": "custom",
                "id": "abandoned-retrieval",
                "parentId": "user",
                "customType": "synth.memory.retrieval",
                "data": {"itemIds": [abandoned_item_id]},
            }
        )
    records.extend(
        [
            {
                "type": "custom",
                "id": "active-retrieval",
                "parentId": "user",
                "customType": "synth.memory.retrieval",
                "data": {
                    "taskQuery": task,
                    "section": "## Lessons from past sessions\nFixture",
                    "itemIds": item_ids,
                    "framingSection": (
                        "## Problem framing\nFixture" if framing_item_ids else None
                    ),
                    "framingItemIds": framing_item_ids,
                    "timestamp": 1,
                },
            },
            {
                "type": "message",
                "id": "assistant",
                "parentId": "active-retrieval",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "The task is complete."}],
                },
            },
            {
                "type": "agent_status",
                "id": "status",
                "parentId": "assistant",
                "status": {"taskState": "completed"},
            },
            {
                "type": "session_state",
                "id": "state",
                "parentId": "status",
                "state": {"status": "archived"},
            },
        ]
    )
    path = workspace / f"{session_id}.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def _write_terminal_outcome(workspace: Path, session_id: str, *, reward: int) -> None:
    telemetry_dir = workspace / ".synth" / "telemetry"
    telemetry_dir.mkdir(parents=True, exist_ok=True)
    reason = (
        TerminationReason.SUCCESS
        if reward == 1
        else TerminationReason.FAILED_ASSERTION
    )
    outcome = TerminalOutcome(
        reward,
        reason,
        OutcomeEvidence("artifact", str(workspace / f"{session_id}-acceptance.json")),
    )
    event = {
        "schemaVersion": 1,
        "eventId": f"{session_id}-outcome",
        "sequence": 1,
        "timestamp": "2026-08-26T00:00:00.000Z",
        "sessionId": session_id,
        "kind": "session.outcome",
        "outcome": outcome.as_dict(),
    }
    (telemetry_dir / f"{session_id}.jsonl").write_text(json.dumps(event) + "\n")
