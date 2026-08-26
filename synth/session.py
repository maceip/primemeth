"""Active-branch parsing for Prime Agent v3 session trajectories."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import cast

from local_memory.observation_redactor import redact_tool_observation

_BUDGET_BLOCK_RE = re.compile(r"\n?<budget>.*?</budget>\n?", re.DOTALL)
_BUDGET_STATUS_RE = re.compile(r"\n?\[Budget:.*?\| Regime: [A-Z]+\]\n?")
_STABLE_INTERVAL_SECONDS = 0.05
_STABLE_OBSERVATIONS = 3
_STABLE_TIMEOUT_SECONDS = 2.0
_LESSONS_HEADING = "## Lessons from past sessions"
_PROBLEM_FRAMING_HEADING = "## Problem framing"


class SessionParseError(ValueError):
    """Raised when a session cannot provide a trustworthy active trajectory."""


@dataclass(frozen=True, slots=True)
class SessionTrajectory:
    session_id: str
    workspace: Path
    leaf_id: str
    task_query: str
    trajectory: str
    final_state: str
    response: str | None
    task_state: str | None
    message_count: int
    retrieved_memory_ids: tuple[int, ...] = ()


def wait_for_stable_session(path: Path) -> None:
    deadline = time.monotonic() + _STABLE_TIMEOUT_SECONDS
    prior: tuple[int, int] | None = None
    unchanged = 0
    while time.monotonic() < deadline:
        try:
            stat = path.stat()
        except OSError as error:
            raise SessionParseError(f"cannot stat session {path}: {error}") from error
        current = (stat.st_size, stat.st_mtime_ns)
        if current == prior:
            unchanged += 1
            if unchanged >= _STABLE_OBSERVATIONS:
                return
        else:
            prior = current
            unchanged = 0
        time.sleep(_STABLE_INTERVAL_SECONDS)
    raise SessionParseError(f"session did not become stable: {path}")


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SessionParseError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _text_content(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for raw_part in value:
        if not isinstance(raw_part, dict):
            continue
        part = cast(dict[str, object], raw_part)
        part_type = part.get("type")
        if part_type == "text" and isinstance(part.get("text"), str):
            parts.append(cast(str, part["text"]))
        elif part_type == "thinking" and isinstance(part.get("thinking"), str):
            parts.append(f"Thinking: {part['thinking']}")
        elif part_type == "toolCall":
            name = part.get("name")
            arguments = part.get("arguments")
            if isinstance(name, str):
                rendered_arguments = json.dumps(
                    arguments, ensure_ascii=False, sort_keys=True
                )
                parts.append(f"Tool call {name}: {rendered_arguments}")
    return "\n".join(parts)


def strip_budget_context(text: str) -> str:
    without_block = _BUDGET_BLOCK_RE.sub("\n", text)
    return _BUDGET_STATUS_RE.sub("\n", without_block).strip()


def _active_branch(entries: list[dict[str, object]]) -> list[dict[str, object]]:
    by_id: dict[str, dict[str, object]] = {}
    leaf_id: str | None = None
    for entry in entries:
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            raise SessionParseError("v3 session entry has no id")
        if entry_id in by_id:
            raise SessionParseError(f"duplicate session entry id: {entry_id}")
        by_id[entry_id] = entry
        leaf_id = entry_id
    if leaf_id is None:
        raise SessionParseError("session has no trajectory entries")

    reversed_branch: list[dict[str, object]] = []
    seen: set[str] = set()
    current: str | None = leaf_id
    while current is not None:
        if current in seen:
            raise SessionParseError("session parent chain contains a cycle")
        seen.add(current)
        current_entry = by_id.get(current)
        if current_entry is None:
            raise SessionParseError(f"session parent entry is missing: {current}")
        reversed_branch.append(current_entry)
        parent = current_entry.get("parentId")
        if parent is not None and not isinstance(parent, str):
            raise SessionParseError(f"entry {current} has an invalid parentId")
        current = parent
    return list(reversed(reversed_branch))


def _render_message(
    entry: dict[str, object], *, redact_observations: bool
) -> tuple[str, str] | None:
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    value = cast(dict[str, object], message)
    role = value.get("role")
    if not isinstance(role, str):
        return None
    if role == "bashExecution":
        command = value.get("command")
        output = value.get("output")
        exit_code = value.get("exitCode")
        normalized_output = strip_budget_context(str(output))
        if redact_observations:
            normalized_output = redact_tool_observation(normalized_output)
        text = (
            f"Command: {command}\nExit code: {exit_code}\nOutput:\n"
            f"{normalized_output}"
        )
    else:
        text = _text_content(value.get("content"))
    normalized = strip_budget_context(text)
    if role == "toolResult" and redact_observations:
        normalized = redact_tool_observation(normalized)
    if not normalized:
        return None
    label = {
        "user": "User",
        "assistant": "Assistant",
        "toolResult": f"Tool result ({value.get('toolName', 'unknown')})",
        "bashExecution": "Shell execution",
        "custom": "Extension message",
    }.get(role, role)
    return role, f"{label}:\n{normalized}"


def _retrieved_strategy_ids(entry: dict[str, object]) -> tuple[int, ...] | None:
    if entry.get("type") != "custom" or entry.get("customType") != (
        "synth.memory.retrieval"
    ):
        return None
    data = entry.get("data")
    if not isinstance(data, dict):
        return None
    task_query = data.get("taskQuery")
    timestamp = data.get("timestamp")
    section = data.get("section")
    framing_section = data.get("framingSection")
    item_ids = data.get("itemIds")
    framing_item_ids = data.get("framingItemIds", [])
    if (
        not isinstance(task_query, str)
        or not task_query
        or not isinstance(timestamp, (int, float))
        or isinstance(timestamp, bool)
        or not isfinite(float(timestamp))
        or not isinstance(item_ids, list)
        or not isinstance(framing_item_ids, list)
    ):
        return None
    if not all(
        isinstance(memory_id, int)
        and not isinstance(memory_id, bool)
        and memory_id > 0
        for memory_id in item_ids
    ) or not all(
        isinstance(memory_id, int)
        and not isinstance(memory_id, bool)
        and memory_id > 0
        for memory_id in framing_item_ids
    ):
        return None
    valid_section = (
        isinstance(section, str) and section.startswith(_LESSONS_HEADING)
    )
    valid_framing = isinstance(framing_section, str) and framing_section.startswith(
        _PROBLEM_FRAMING_HEADING
    )
    if (
        valid_section != bool(item_ids)
        or valid_framing != bool(framing_item_ids)
        or (not valid_section and not valid_framing)
    ):
        return None
    return tuple(dict.fromkeys(cast(list[int], item_ids)))


def load_session_trajectory(
    path: Path,
    *,
    wait_for_stability: bool = False,
    redact_observations: bool = False,
) -> SessionTrajectory:
    resolved = path.expanduser().resolve()
    if wait_for_stability:
        wait_for_stable_session(resolved)
    if not resolved.is_file():
        raise SessionParseError(f"session is not a file: {resolved}")
    parsed: list[dict[str, object]] = []
    try:
        for line_number, line in enumerate(resolved.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value: object = json.loads(line)
            except json.JSONDecodeError as error:
                raise SessionParseError(
                    f"session line {line_number} is invalid JSON"
                ) from error
            parsed.append(_mapping(value, f"session line {line_number}"))
    except OSError as error:
        raise SessionParseError(f"cannot read session {resolved}: {error}") from error
    if not parsed:
        raise SessionParseError("session is empty")
    header = parsed[0]
    if header.get("type") != "session" or header.get("version") != 3:
        raise SessionParseError("ReasoningBank requires a Prime Agent v3 session")
    session_id = header.get("id")
    workspace = header.get("cwd")
    if not isinstance(session_id, str) or not session_id:
        raise SessionParseError("session header has no id")
    if not isinstance(workspace, str) or not workspace:
        raise SessionParseError("session header has no cwd")

    branch = _active_branch(parsed[1:])
    task_query = ""
    response: str | None = None
    task_state: str | None = None
    session_state = "unknown"
    trajectory_parts: list[str] = []
    message_count = 0
    retrieved_memory_ids: tuple[int, ...] = ()
    for entry in branch:
        entry_type = entry.get("type")
        if entry_type == "message":
            rendered = _render_message(
                entry,
                redact_observations=redact_observations,
            )
            if rendered is None:
                continue
            role, text = rendered
            message_count += 1
            trajectory_parts.append(text)
            if role == "user" and not task_query:
                message = cast(dict[str, object], entry["message"])
                task_query = strip_budget_context(_text_content(message.get("content")))
            elif role == "assistant":
                response = text.removeprefix("Assistant:\n").strip() or None
        elif entry_type == "compaction" and isinstance(entry.get("summary"), str):
            trajectory_parts.append(f"Compaction summary:\n{entry['summary']}")
        elif entry_type == "branch_summary" and isinstance(entry.get("summary"), str):
            trajectory_parts.append(f"Branch summary:\n{entry['summary']}")
        elif entry_type == "agent_status":
            status = entry.get("status")
            if isinstance(status, dict):
                candidate = status.get("taskState")
                if candidate in {"completed", "needs_input"}:
                    task_state = cast(str, candidate)
        elif entry_type == "session_state":
            state = entry.get("state")
            if isinstance(state, dict) and isinstance(state.get("status"), str):
                session_state = cast(str, state["status"])
        else:
            candidate_ids = _retrieved_strategy_ids(entry)
            if candidate_ids is not None:
                retrieved_memory_ids = candidate_ids
    if not task_query:
        raise SessionParseError("active session branch has no user task")
    if not trajectory_parts:
        raise SessionParseError("active session branch has no trajectory")
    return SessionTrajectory(
        session_id=session_id,
        workspace=Path(workspace).expanduser().resolve(),
        leaf_id=cast(str, branch[-1]["id"]),
        task_query=task_query,
        trajectory="\n\n".join(trajectory_parts),
        final_state=f"Native session lifecycle state: {session_state}",
        response=response,
        task_state=task_state,
        message_count=message_count,
        retrieved_memory_ids=retrieved_memory_ids,
    )
