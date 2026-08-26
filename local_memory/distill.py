"""ReasoningBank trajectory distillation with strict parsing and deduplication."""

from __future__ import annotations

import re
from dataclasses import dataclass

from local_memory.dedup import DedupResult, MemoryDeduplicator
from local_memory.judge import JudgeVerdict, ModelCallable
from local_memory.observation_redactor import (
    DEFAULT_MAX_TRAJECTORY_TOKENS,
    cap_trajectory_for_prompt,
)
from local_memory.prompts_reasoning_bank import (
    FAILED_SI,
    MAX_MEMORY_ITEMS,
    SUCCESSFUL_SI,
)
from local_memory.store import MemoryItemType, MemoryStatus, NewMemory

_MEMORY_HEADER_RE = re.compile(r"^#\s+Memory Item\s+\d+\s*$", re.I | re.M)
_FIELD_HEADER_RE = re.compile(
    r"^##\s+(Title|Description|Content)(?:[ \t]+(.*?))?[ \t]*$", re.I
)


@dataclass(frozen=True, slots=True)
class ParsedMemory:
    title: str
    description: str
    content: str


@dataclass(frozen=True, slots=True)
class ParsedMemoryResponse:
    items: tuple[ParsedMemory, ...]
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DistillationResult:
    outcomes: tuple[DedupResult, ...]
    parse_errors: tuple[str, ...]
    raw_response: str
    prompt: str

    @property
    def inserted_count(self) -> int:
        return sum(outcome.action == "inserted" for outcome in self.outcomes)


def build_extraction_prompt(
    *,
    task_query: str,
    trajectory: str,
    verdict: JudgeVerdict,
    judge_reasoning: str | None = None,
    max_trajectory_tokens: int = DEFAULT_MAX_TRAJECTORY_TOKENS,
) -> tuple[str, str]:
    if not task_query.strip():
        raise ValueError("task_query cannot be empty")
    if not trajectory.strip():
        raise ValueError("trajectory cannot be empty")
    if verdict not in ("success", "failure"):
        raise ValueError(f"unsupported judge verdict: {verdict}")
    system_prompt = SUCCESSFUL_SI if verdict == "success" else FAILED_SI
    prompt_trajectory = cap_trajectory_for_prompt(
        trajectory,
        max_tokens=max_trajectory_tokens,
    )
    prompt = f"**Query:** {task_query.strip()}\n\n**Trajectory:**\n{prompt_trajectory}"
    if judge_reasoning and judge_reasoning.strip():
        status_label = "succeeded" if verdict == "success" else "failed"
        prompt += f"\n\nThe task {status_label} because: {judge_reasoning.strip()}"
    return system_prompt, prompt


def parse_memory_response(
    response: str, *, max_items: int = MAX_MEMORY_ITEMS
) -> ParsedMemoryResponse:
    if max_items <= 0:
        raise ValueError("max_items must be positive")
    matches = list(_MEMORY_HEADER_RE.finditer(response))
    if not matches:
        return ParsedMemoryResponse(
            items=(), errors=("response contains no '# Memory Item i' blocks",)
        )

    errors: list[str] = []
    if len(matches) > max_items:
        errors.append(
            f"response contains {len(matches)} items; only the first "
            f"{max_items} were accepted"
        )

    parsed: list[ParsedMemory] = []
    for ordinal, match in enumerate(matches[:max_items], start=1):
        end = matches[ordinal].start() if ordinal < len(matches) else len(response)
        fields, field_errors = _parse_fields(response[match.end() : end], ordinal)
        errors.extend(field_errors)
        if field_errors:
            continue
        parsed.append(
            ParsedMemory(
                title=fields["title"],
                description=fields["description"],
                content=fields["content"],
            )
        )
    return ParsedMemoryResponse(items=tuple(parsed), errors=tuple(errors))


class MemoryDistiller:
    def __init__(
        self,
        model_call: ModelCallable,
        deduplicator: MemoryDeduplicator,
        *,
        max_trajectory_tokens: int = DEFAULT_MAX_TRAJECTORY_TOKENS,
    ) -> None:
        self.model_call = model_call
        self.deduplicator = deduplicator
        self.max_trajectory_tokens = max_trajectory_tokens

    def distill(
        self,
        *,
        task_query: str,
        trajectory: str,
        verdict: JudgeVerdict,
        session_id: str,
        template_key: str | None = None,
        item_type: MemoryItemType = "strategy",
        judge_reasoning: str | None = None,
    ) -> DistillationResult:
        if not session_id.strip():
            raise ValueError("session_id cannot be empty")
        system_prompt, prompt = build_extraction_prompt(
            task_query=task_query,
            trajectory=trajectory,
            verdict=verdict,
            judge_reasoning=judge_reasoning,
            max_trajectory_tokens=self.max_trajectory_tokens,
        )
        raw_response = self.model_call(system_prompt, prompt)
        parsed = parse_memory_response(raw_response)
        status: MemoryStatus = "success" if verdict == "success" else "fail"
        outcomes = tuple(
            self.deduplicator.add(
                NewMemory(
                    title=item.title,
                    description=item.description,
                    content=item.content,
                    status=status,
                    item_type=item_type,
                    task_query=task_query,
                    session_id=session_id,
                    template_key=template_key,
                )
            )
            for item in parsed.items
        )
        return DistillationResult(
            outcomes=outcomes,
            parse_errors=parsed.errors,
            raw_response=raw_response,
            prompt=prompt,
        )


def _parse_fields(block: str, ordinal: int) -> tuple[dict[str, str], list[str]]:
    values: dict[str, list[str]] = {}
    current_field: str | None = None
    errors: list[str] = []
    for line in block.splitlines():
        header = _FIELD_HEADER_RE.fullmatch(line.strip())
        if header is not None:
            field = header.group(1).lower()
            if field in values:
                errors.append(f"item {ordinal} repeats the {field} field")
            values.setdefault(field, [])
            current_field = field
            inline_value = (header.group(2) or "").strip()
            if inline_value:
                values[field].append(inline_value)
            continue
        if current_field is not None:
            values[current_field].append(line)

    normalized: dict[str, str] = {}
    for field in ("title", "description", "content"):
        value = "\n".join(values.get(field, [])).strip()
        if not value:
            errors.append(f"item {ordinal} is missing {field}")
        elif value.startswith("<") and value.endswith(">"):
            errors.append(f"item {ordinal} contains a placeholder {field}")
        else:
            normalized[field] = value
    return normalized, errors
