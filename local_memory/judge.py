"""Injected-LLM trajectory judge using the pinned ReasoningBank contract."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast

from local_memory.observation_redactor import (
    DEFAULT_MAX_TRAJECTORY_TOKENS,
    cap_trajectory_for_prompt,
)
from local_memory.prompts_reasoning_bank import (
    AUTOEVAL_TRAJECTORY_JUDGE_PROMPT,
    AUTOEVAL_TRAJECTORY_JUDGE_SYSTEM_PROMPT,
)
from middleware.terminal_outcome import TerminalOutcome

JudgeVerdict = Literal["success", "failure"]
ModelCallable = Callable[[str, str], str]
_STATUS_RE = re.compile(r'^Status:\s*["\']?(success|failure)["\']?\s*$', re.I)


class JudgeResponseError(ValueError):
    """Raised when the model does not honor the two-line verdict contract."""


@dataclass(frozen=True, slots=True)
class JudgeResult:
    verdict: JudgeVerdict
    thoughts: str
    raw_response: str
    prompt: str


def parse_judge_response(response: str) -> tuple[JudgeVerdict, str]:
    lines = [line.strip() for line in response.strip().splitlines() if line.strip()]
    if len(lines) != 2 or not lines[0].startswith("Thoughts:"):
        raise JudgeResponseError(
            "judge response must contain exactly Thoughts and Status lines"
        )
    thoughts = lines[0][len("Thoughts:") :].strip().rstrip('"').strip()
    if not thoughts:
        raise JudgeResponseError("judge response has empty Thoughts")
    status_match = _STATUS_RE.fullmatch(lines[1])
    if status_match is None:
        raise JudgeResponseError('judge Status must be exactly "success" or "failure"')
    verdict = cast(JudgeVerdict, status_match.group(1).lower())
    return verdict, thoughts


class TrajectoryJudge:
    """Judge observed evidence; native execution priors remain prompt context only."""

    def __init__(
        self,
        model_call: ModelCallable,
        *,
        max_trajectory_tokens: int = DEFAULT_MAX_TRAJECTORY_TOKENS,
    ) -> None:
        self.model_call = model_call
        self.max_trajectory_tokens = max_trajectory_tokens

    def judge(
        self,
        *,
        task_query: str,
        trajectory: str,
        final_state: str = "N/A",
        response: str | None = None,
        task_state: str | None = None,
        exit_code: int | None = None,
        terminal_outcome: TerminalOutcome | None = None,
    ) -> JudgeResult:
        if not task_query.strip():
            raise ValueError("task_query cannot be empty")
        if not trajectory.strip():
            raise ValueError("trajectory cannot be empty")
        prompt_trajectory = cap_trajectory_for_prompt(
            trajectory,
            max_tokens=self.max_trajectory_tokens,
        )
        prompt = AUTOEVAL_TRAJECTORY_JUDGE_PROMPT.format(
            intent=task_query.strip(),
            last_actions=prompt_trajectory,
            cap=final_state.strip() or "N/A",
            response=response.strip() if response and response.strip() else "N/A",
        )
        prompt += (
            "\n\nNative execution priors (context only, not the verdict):\n"
            f"Task state: {task_state if task_state is not None else 'unknown'}\n"
            f"Exit code: {exit_code if exit_code is not None else 'unknown'}\n"
            + (
                "Terminal outcome: unknown"
                if terminal_outcome is None
                else (
                    f"Terminal reward: {terminal_outcome.reward}\n"
                    "Terminal reason: "
                    f"{terminal_outcome.termination_reason.value}\n"
                    f"Terminal evidence: {terminal_outcome.evidence.uri}"
                )
            )
        )
        raw_response = self.model_call(AUTOEVAL_TRAJECTORY_JUDGE_SYSTEM_PROMPT, prompt)
        verdict, thoughts = parse_judge_response(raw_response)
        return JudgeResult(
            verdict=verdict,
            thoughts=thoughts,
            raw_response=raw_response,
            prompt=prompt,
        )
