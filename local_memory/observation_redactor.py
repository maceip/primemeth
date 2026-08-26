"""Deterministic failure-preserving redaction for memory prompts."""

from __future__ import annotations

import re
from collections.abc import Sequence

OBSERVATION_HEAD_LINES = 50
OBSERVATION_TAIL_LINES = 150
MAX_EXTRACTED_EVIDENCE_LINES = 96
MAX_LOG_LINE_BYTES = 8_192
DEFAULT_MAX_TRAJECTORY_TOKENS = 64_000

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_EXIT_CODE_RES = (
    re.compile(
        r"(?i)^\s*exit(?:ed)?(?:\s+(?:with\s+)?)?"
        r"(?:code|status)\s*[:=]?\s*-?\d+\b"
    ),
    re.compile(
        r"(?i)^\s*[\"']?(?:exit[_-]?code|exitCode|returncode)"
        r"[\"']?\s*[:=]\s*-?\d+\b"
    ),
    re.compile(
        r"(?i)^\s*(?:process|command|pytest|test runner)\b.*"
        r"\bexited?\s+(?:with\s+)?(?:code\s+)?-?\d+\b"
    ),
    re.compile(r"(?i)\breturned non[- ]zero exit status\s+-?\d+\b"),
)
_TRACEBACK_START_RE = re.compile(r"^\s*Traceback \(most recent call last\):\s*$")
_PYTHON_FRAME_RE = re.compile(
    r'^\s*File ".+?", line \d+(?:, in .+)?\s*$'
)
_PYTHON_CHAIN_RE = re.compile(
    r"^\s*(?:During handling of the above exception, another exception occurred:|"
    r"The above exception was the direct cause of the following exception:)\s*$"
)
_JAVASCRIPT_FRAME_RE = re.compile(
    r"^\s*at\s+(?:async\s+)?(?:.+?\s+\()?"
    r"(?:file://)?[^()\s]+:\d+:\d+\)?\s*$"
)
_BACKTRACE_FRAME_RE = re.compile(
    r"^\s*(?:stack backtrace:|#?\d+:?\s+(?:0x[0-9a-f]+\s+)?(?:in\s+)?\S+)"
    r".*$",
    re.IGNORECASE,
)
_TERMINAL_EXCEPTION_RE = re.compile(
    r"^\s*(?:E\s+)?(?:"
    r"(?:(?:[A-Za-z_$][\w$]*\.)*[A-Za-z_$][\w$]*"
    r"(?:Error|Exception|Failure|Fault|Interrupt|Abort))"
    r"(?:\s+\[[A-Z0-9_]+\])?(?::.*)?|"
    r"SystemExit(?::.*)?|KeyboardInterrupt(?::.*)?|"
    r"(?:Uncaught(?:\s+\(in promise\))?\s+)?"
    r"(?:(?:Error|Exception)|AssertionError(?:\s+\[[A-Z0-9_]+\])?|"
    r"[A-Za-z_$][\w.$]*(?:Error|Exception))(?::.*)?|"
    r"(?:panic(?:ked)?|fatal(?: error)?|caused by)\s*:.*|"
    r"thread\s+.+\s+panicked at.*"
    r")\s*$",
    re.IGNORECASE,
)


def conservative_token_upper_bound(text: str) -> int:
    """Return a tokenizer-free upper bound based on normalized UTF-8 bytes."""

    return len(text.encode("utf-8", errors="replace"))


def redact_tool_observation(text: str) -> str:
    """Keep observation boundaries and reinsert bounded failure evidence."""

    lines = [_sanitize_line(line) for line in text.splitlines()]
    if len(lines) <= OBSERVATION_HEAD_LINES + OBSERVATION_TAIL_LINES:
        return "\n".join(lines)
    middle_start = OBSERVATION_HEAD_LINES
    middle_end = len(lines) - OBSERVATION_TAIL_LINES
    evidence = extract_failure_evidence(lines[middle_start:middle_end])
    marker = [
        "<observation_omitted "
        f'lines="{middle_end - middle_start}" '
        f'extracted="{len(evidence)}">'
    ]
    if evidence:
        marker.extend(evidence)
    marker.append("</observation_omitted>")
    return "\n".join(
        [
            *lines[:middle_start],
            *marker,
            *lines[middle_end:],
        ]
    )


def extract_failure_evidence(lines: Sequence[str]) -> tuple[str, ...]:
    """Extract ordered process and stack evidence from an omitted line span."""

    selected: set[int] = set()
    terminal_indices: list[int] = []
    for index, original in enumerate(lines):
        candidate = _ANSI_RE.sub("", original)
        if any(pattern.search(candidate) is not None for pattern in _EXIT_CODE_RES):
            selected.add(index)
        python_frame = _PYTHON_FRAME_RE.fullmatch(candidate) is not None
        if (
            _TRACEBACK_START_RE.fullmatch(candidate)
            or python_frame
            or _PYTHON_CHAIN_RE.fullmatch(candidate)
            or _JAVASCRIPT_FRAME_RE.fullmatch(candidate)
            or _BACKTRACE_FRAME_RE.fullmatch(candidate)
        ):
            selected.add(index)
        if python_frame and index + 1 < len(lines):
            source_line = _ANSI_RE.sub("", lines[index + 1])
            if source_line.startswith((" ", "\t")):
                selected.add(index + 1)
        if _TERMINAL_EXCEPTION_RE.fullmatch(candidate):
            selected.add(index)
            terminal_indices.append(index)

    ordered = sorted(selected)
    if len(ordered) > MAX_EXTRACTED_EVIDENCE_LINES:
        half = MAX_EXTRACTED_EVIDENCE_LINES // 2
        ordered = [*ordered[:half], *ordered[-half:]]
    if terminal_indices:
        terminal = terminal_indices[-1]
        if terminal not in ordered:
            ordered = [*ordered[:-1], terminal]
            ordered.sort()

    evidence: list[str] = []
    seen: set[str] = set()
    for index in ordered:
        line = _sanitize_line(lines[index])
        if line in seen:
            continue
        seen.add(line)
        evidence.append(line)
    return tuple(evidence)


def cap_trajectory_for_prompt(trajectory: str, *, max_tokens: int) -> str:
    """Cap a trajectory using a conservative byte-per-token upper bound."""

    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    normalized = trajectory.strip()
    original_tokens = conservative_token_upper_bound(normalized)
    if original_tokens <= max_tokens:
        return normalized

    evidence_lines = list(extract_failure_evidence(normalized.splitlines()))
    marker = (
        "\n\n<trajectory_omitted "
        f'original_token_upper_bound="{original_tokens}" '
        f'max_tokens="{max_tokens}">\n'
    )
    marker_end = "\n</trajectory_omitted>\n\n"
    fixed_without_evidence = marker + marker_end
    fixed_tokens = conservative_token_upper_bound(fixed_without_evidence)
    if fixed_tokens >= max_tokens:
        return _truncate_utf8_both(fixed_without_evidence, max_tokens)

    def layout(lines: Sequence[str]) -> tuple[str, str, str]:
        evidence = "\n".join(lines)
        evidence_budget = min(
            conservative_token_upper_bound(evidence),
            max_tokens // 3,
        )
        fitted_evidence = _truncate_utf8_both(evidence, evidence_budget)
        fixed = marker + fitted_evidence + marker_end
        remaining = max_tokens - conservative_token_upper_bound(fixed)
        head_budget = remaining // 2
        tail_budget = remaining - head_budget
        head = _truncate_utf8_prefix(normalized, head_budget).rstrip()
        tail = _truncate_utf8_suffix(normalized, tail_budget).lstrip()
        return head, fixed, tail

    for _iteration in range(4):
        head, fixed, tail = layout(evidence_lines)
        visible = f"{head}\n{tail}"
        filtered = [line for line in evidence_lines if line not in visible]
        if filtered == evidence_lines:
            break
        evidence_lines = filtered
    head, fixed, tail = layout(evidence_lines)
    capped = head + fixed + tail
    if conservative_token_upper_bound(capped) > max_tokens:
        raise AssertionError("trajectory redaction exceeded its deterministic cap")
    return capped


def _sanitize_line(line: str) -> str:
    without_ansi = _ANSI_RE.sub("", line)
    sanitized = "".join(
        character
        if character == "\t" or ord(character) >= 32
        else "\ufffd"
        for character in without_ansi
    )
    if conservative_token_upper_bound(sanitized) <= MAX_LOG_LINE_BYTES:
        return sanitized
    return _truncate_utf8_both(sanitized, MAX_LOG_LINE_BYTES)


def _truncate_utf8_prefix(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


def _truncate_utf8_suffix(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text
    return encoded[-limit:].decode("utf-8", errors="ignore")


def _truncate_utf8_both(text: str, limit: int) -> str:
    if conservative_token_upper_bound(text) <= limit:
        return text
    separator = "\n...\n"
    separator_tokens = conservative_token_upper_bound(separator)
    if limit <= separator_tokens:
        return _truncate_utf8_prefix(text, limit)
    content_budget = limit - separator_tokens
    head_budget = content_budget // 2
    tail_budget = content_budget - head_budget
    return (
        _truncate_utf8_prefix(text, head_budget)
        + separator
        + _truncate_utf8_suffix(text, tail_budget)
    )
