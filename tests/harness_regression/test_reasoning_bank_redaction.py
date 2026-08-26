from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

import synth.reasoning_bank as reasoning_bank
from local_memory.embedder import EmbeddingBatch
from local_memory.observation_redactor import (
    cap_trajectory_for_prompt,
    conservative_token_upper_bound,
    redact_tool_observation,
)
from local_memory.prompts_reasoning_bank import (
    AUTOEVAL_TRAJECTORY_JUDGE_SYSTEM_PROMPT,
    FAILED_SI,
)
from synth.reasoning_bank import distill_session
from synth.session import load_session_trajectory

_MAX_TRAJECTORY_TOKENS = 8_000
_TERMINAL_EXCEPTION = "InvariantViolationError: REDACTION_SENTINEL_5000"


class OfflineEmbedder:
    def __init__(self, _workspace: Path, *, model_name: str) -> None:
        self.model_name = model_name

    def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        vector = [1.0, *([0.0] * 383)]
        return EmbeddingBatch(
            model=self.model_name,
            dimension=384,
            embeddings=[vector.copy() for _text in texts],
        )


def test_5000_line_failure_is_redacted_capped_and_distilled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_path = _write_failure_session(tmp_path)
    trajectory = load_session_trajectory(
        session_path,
        redact_observations=True,
    )

    assert "HEAD-000" in trajectory.trajectory
    assert "HEAD-049" in trajectory.trajectory
    assert "TAIL-4850" in trajectory.trajectory
    assert "TAIL-4999" in trajectory.trajectory
    assert "FILLER-1200" not in trajectory.trajectory
    assert "Traceback (most recent call last):" in trajectory.trajectory
    assert 'File "/workspace/example.py", line 42, in verify' in trajectory.trajectory
    assert _TERMINAL_EXCEPTION in trajectory.trajectory
    assert "Exit code: 17" in trajectory.trajectory
    assert trajectory.trajectory.count("REDACTION_SENTINEL_5000") == 1
    assert '<observation_omitted lines="4800" extracted="4">' in trajectory.trajectory

    capped = cap_trajectory_for_prompt(
        trajectory.trajectory,
        max_tokens=_MAX_TRAJECTORY_TOKENS,
    )
    assert conservative_token_upper_bound(capped) <= _MAX_TRAJECTORY_TOKENS
    assert _TERMINAL_EXCEPTION in capped
    assert "<trajectory_omitted" in capped

    judge_prompts: list[str] = []
    distill_prompts: list[str] = []

    def model_call(system_prompt: str, prompt: str) -> str:
        if system_prompt == AUTOEVAL_TRAJECTORY_JUDGE_SYSTEM_PROMPT:
            judge_prompts.append(prompt)
            return (
                "Thoughts: The terminal traceback proves the requested repair failed.\n"
                'Status: "failure"'
            )
        if system_prompt == FAILED_SI:
            distill_prompts.append(prompt)
            return (
                "# Memory Item 1\n"
                "## Title Inspect the terminal exception before revising code\n"
                "## Description Use when a long tool log hides the decisive failure.\n"
                "## Content Preserve boundary evidence, then diagnose the final "
                "exception and nonzero process exit.\n"
            )
        raise AssertionError(f"unexpected model prompt: {system_prompt[:80]}")

    monkeypatch.setattr(
        reasoning_bank,
        "configured_model_callable",
        lambda _settings, _fixture_path: model_call,
    )
    monkeypatch.setattr(reasoning_bank, "LocalEmbedder", OfflineEmbedder)

    result = distill_session(session_path)

    assert result["inserted"] == 1
    assert result["parseErrors"] == []
    assert len(judge_prompts) == 1
    assert len(distill_prompts) == 1
    for prompt in (*judge_prompts, *distill_prompts):
        assert _TERMINAL_EXCEPTION in prompt
        assert prompt.count("REDACTION_SENTINEL_5000") == 1
        assert "Traceback (most recent call last):" in prompt
        assert "Exit code: 17" in prompt
        assert "FILLER-1200" not in prompt
        assert "<trajectory_omitted" in prompt


@pytest.mark.parametrize(
    "line",
    [
        "Exit code: 9",
        "returncode=-2",
        "Process exited with code 17",
        "returned non-zero exit status 3",
    ],
)
def test_redactor_extracts_supported_exit_code_forms(line: str) -> None:
    observation = "\n".join(
        [
            *(f"head-{index}" for index in range(50)),
            *(f"middle-{index}" for index in range(20)),
            line,
            *(f"middle-tail-{index}" for index in range(20)),
            *(f"tail-{index}" for index in range(150)),
        ]
    )
    redacted = redact_tool_observation(observation)
    assert line in redacted
    assert redacted.count(line) == 1


def test_redactor_does_not_treat_http_status_as_process_exit() -> None:
    lines = [*(f"head-{index}" for index in range(50))]
    lines.extend(["HTTP status: 500", *(f"middle-{index}" for index in range(40))])
    lines.extend(f"tail-{index}" for index in range(150))
    redacted = redact_tool_observation("\n".join(lines))
    assert "HTTP status: 500" not in redacted


def test_redactor_extracts_ansi_node_terminal_exception_and_stack() -> None:
    lines = [*(f"head-{index}" for index in range(50))]
    lines.extend(
        [
            "middle-before",
            "\x1b[31mError: NODE_REDACTION_SENTINEL\x1b[0m",
            "    at verify (file:///workspace/example.ts:42:7)",
            "middle-after",
        ]
    )
    lines.extend(f"tail-{index}" for index in range(150))
    redacted = redact_tool_observation("\n".join(lines))
    assert "Error: NODE_REDACTION_SENTINEL" in redacted
    assert "at verify (file:///workspace/example.ts:42:7)" in redacted
    assert "\x1b" not in redacted


def test_redactor_preserves_short_observation_and_caps_one_long_line() -> None:
    short = "\n".join(f"line-{index}" for index in range(200))
    assert redact_tool_observation(short) == short

    long_line = "prefix-" + ("\U0001f680" * 10_000) + "-suffix"
    redacted = redact_tool_observation(long_line)
    assert conservative_token_upper_bound(redacted) <= 8_192
    assert redacted.startswith("prefix-")
    assert redacted.endswith("-suffix")


def _write_failure_session(workspace: Path) -> Path:
    (workspace / "synth.json").write_text(
        json.dumps(
            {
                "memory": {
                    "enabled": True,
                    "k": 3,
                    "minScore": 0.35,
                    "db": ".synth/synth.db",
                    "embedModel": "offline-redaction-fixture",
                    "judgeModel": "small",
                    "maxTrajectoryTokens": _MAX_TRAJECTORY_TOKENS,
                }
            }
        )
    )
    log_lines = [
        f"FILLER-{index:04d}-" + ("x" * 60)
        for index in range(5_000)
    ]
    for index in range(50):
        log_lines[index] = f"HEAD-{index:03d}-" + ("h" * 60)
    for index in range(4_850, 5_000):
        log_lines[index] = f"TAIL-{index:04d}-" + ("t" * 60)
    log_lines[2_497] = "Traceback (most recent call last):"
    log_lines[2_498] = '  File "/workspace/example.py", line 42, in verify'
    log_lines[2_499] = _TERMINAL_EXCEPTION
    log_lines[2_600] = "Exit code: 17"

    records: list[dict[str, object]] = [
        {
            "type": "session",
            "version": 3,
            "id": "redaction-session",
            "cwd": str(workspace),
            "parentSession": None,
        },
        {
            "type": "message",
            "id": "user",
            "parentId": None,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Repair the verification failure in the fixture.",
                    }
                ],
            },
        },
        {
            "type": "message",
            "id": "assistant-tool",
            "parentId": "user",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "name": "bash",
                        "arguments": {"command": "pytest -q"},
                    }
                ],
            },
        },
        {
            "type": "message",
            "id": "tool-result",
            "parentId": "assistant-tool",
            "message": {
                "role": "toolResult",
                "toolName": "bash",
                "content": [{"type": "text", "text": "\n".join(log_lines)}],
            },
        },
        {
            "type": "message",
            "id": "assistant-final",
            "parentId": "tool-result",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "The attempted repair still fails acceptance.",
                    }
                ],
            },
        },
        {
            "type": "agent_status",
            "id": "status",
            "parentId": "assistant-final",
            "status": {"taskState": "completed"},
        },
        {
            "type": "session_state",
            "id": "state",
            "parentId": "status",
            "state": {"status": "archived"},
        },
    ]
    session_path = workspace / "redaction-session.jsonl"
    session_path.write_text(
        "".join(json.dumps(record) + "\n" for record in records)
    )
    return session_path
