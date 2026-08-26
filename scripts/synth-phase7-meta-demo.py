"""Deterministic Phase 7 contrastive meta-memory proof."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from local_memory.dedup import MemoryDeduplicator
from local_memory.embedder import EmbeddingBatch
from local_memory.judge import JudgeResult, JudgeVerdict
from local_memory.store import MemoryStore, NewMemory
from synth.config import (
    MemorySettings,
    SkillCompilerSettings,
    SynthSettings,
    TelemetrySettings,
    VerifierSettings,
)
from synth.reasoning_bank import JudgedSession, _contrast_meta_heuristic
from synth.session import SessionTrajectory


class SemanticDemoEmbedder:
    def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        return EmbeddingBatch(
            model="phase7-semantic-demo",
            dimension=3,
            embeddings=[[1.0, 0.0, 0.0] for _text in texts],
        )


def settings(workspace: Path) -> SynthSettings:
    return SynthSettings(
        workspace=workspace,
        endpoint=None,
        telemetry=TelemetrySettings(workspace / ".synth/telemetry"),
        memory=MemorySettings(
            enabled=True,
            top_k=3,
            min_score=0.35,
            database_path=workspace / ".synth/synth.db",
            embed_model="phase7-semantic-demo",
            judge_model="small",
        ),
        verifier=VerifierSettings(
            interactive=False,
            batch=True,
            max_cycles=2,
            continue_generation=True,
            min_budget_fraction=0.2,
        ),
        skill_compiler=SkillCompilerSettings(
            enabled=True,
            max_per_session=2,
            quarantine_path=workspace / ".synth/quarantine",
        ),
        kill_switch=False,
    )


def judged_session(workspace: Path, verdict: JudgeVerdict) -> JudgedSession:
    return JudgedSession(
        trajectory=SessionTrajectory(
            session_id=f"phase7-{verdict}",
            workspace=workspace,
            leaf_id=f"leaf-{verdict}",
            task_query="Repair a repeated output-normalization verification failure",
            trajectory=(
                "The attempt compared raw output without normalizing incidental "
                "whitespace and reported an incorrect mismatch."
            ),
            final_state="The deterministic acceptance check still fails.",
            response="The attempted repair is incomplete.",
            task_state="completed",
            message_count=3,
        ),
        settings=settings(workspace),
        result=JudgeResult(
            verdict=verdict,
            thoughts=(
                "The evidence strategy did not separate semantic output from "
                "formatting noise."
            ),
            raw_response=f'Thoughts: fixture\nStatus: "{verdict}"',
            prompt="phase7 deterministic judge fixture",
        ),
        terminal_outcome=None,
    )


def main() -> None:
    workspace = Path.cwd().resolve()
    model_calls: list[str] = []

    def model_call(system_prompt: str, prompt: str) -> str:
        model_calls.append(prompt)
        if (
            "not a full trajectory" not in prompt
            or "Retrieved Similar Successful Task" not in prompt
        ):
            raise AssertionError("contrast prompt lost successful-task provenance")
        if "problem framing" not in system_prompt.lower():
            raise AssertionError("contrast prompt used the wrong system instruction")
        return "\n".join(
            (
                "# Memory Item 1",
                "## Title Normalize evidence before changing the implementation",
                "## Description Use when representation differences can mask "
                "semantic failures; do not use when formatting is the contract.",
                "## Content Frame acceptance around semantic output first, then "
                "preserve formatting only when the specification requires it.",
            )
        )

    with MemoryStore(
        ":memory:",
        embedder=SemanticDemoEmbedder(),
        embedding_dimension=3,
        workspace=workspace,
    ) as store:
        successful = store.insert(
            NewMemory(
                title="Compare normalized verification evidence",
                description="Normalize incidental output before judging correctness.",
                content="Separate semantic values from irrelevant presentation noise.",
                status="success",
                item_type="strategy",
                task_query="Verify repeated command output deterministically",
                session_id="phase7-prior-success",
            )
        )
        deduplicator = MemoryDeduplicator(store)
        failed = judged_session(workspace, "failure")
        first = _contrast_meta_heuristic(
            failed,
            store,
            model_call,
            deduplicator,
        )
        second = _contrast_meta_heuristic(
            failed,
            store,
            model_call,
            deduplicator,
        )
        success_model_calls = 0

        def forbidden_model_call(_system_prompt: str, _prompt: str) -> str:
            nonlocal success_model_calls
            success_model_calls += 1
            raise AssertionError("successful sessions must not induce failure framing")

        successful_result = _contrast_meta_heuristic(
            judged_session(workspace, "success"),
            store,
            forbidden_model_call,
            deduplicator,
        )
        meta_items = [
            item for item in store.list() if item.item_type == "meta_heuristic"
        ]
        if len(meta_items) != 1:
            raise AssertionError("semantic deduplication did not cap framing memory")
        item = meta_items[0]
        print(
            json.dumps(
                {
                    "firstAction": first.outcomes[0].action,
                    "secondAction": second.outcomes[0].action,
                    "modelCalls": len(model_calls),
                    "successModelCalls": success_model_calls,
                    "successOutcomeCount": len(successful_result.outcomes),
                    "successfulMemoryId": successful.id,
                    "contrastedSuccessfulMemoryId": first.successful_memory_id,
                    "metaCount": len(meta_items),
                    "item": {
                        "id": item.id,
                        "title": item.title,
                        "description": item.description,
                        "content": item.content,
                        "status": item.status,
                        "itemType": item.item_type,
                        "taskQuery": item.task_query,
                        "sessionId": item.session_id,
                        "templateKey": item.template_key,
                        "createdAt": item.created_at,
                        "score": 1.0,
                    },
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
