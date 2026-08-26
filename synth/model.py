"""Local OpenAI-compatible and deterministic fixture model callables."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

from local_memory.prompts_reasoning_bank import (
    AUTOEVAL_TRAJECTORY_JUDGE_SYSTEM_PROMPT,
)
from synth.config import SynthSettings

MODEL_TIMEOUT_SECONDS = 60.0
FIXTURE_ENV = "SYNTH_MEMORY_FIXTURE"


class ModelCallError(RuntimeError):
    """Raised when the configured local model cannot return a valid response."""


def _completion_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ModelCallError(
            "endpoint.baseUrl must be an HTTP(S) URL without credentials, query, "
            "or fragment"
        )
    return f"{base_url.rstrip('/')}/chat/completions"


def _model_id(settings: SynthSettings) -> str:
    endpoint = settings.endpoint
    if endpoint is None:
        raise ModelCallError("endpoint is required for ReasoningBank model calls")
    configured = settings.memory.judge_model
    if configured == "small":
        return endpoint.small_model
    if configured == "main":
        return endpoint.model
    return configured


def local_model_callable(settings: SynthSettings) -> Callable[[str, str], str]:
    endpoint = settings.endpoint
    if endpoint is None:
        raise ModelCallError("endpoint is required for ReasoningBank model calls")
    completion_url = _completion_url(endpoint.base_url)
    model = _model_id(settings)
    opener = build_opener(ProxyHandler({}))

    def call(system_prompt: str, prompt: str) -> str:
        payload = json.dumps(
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "stream": False,
            }
        ).encode()
        headers = {"Content-Type": "application/json"}
        api_key = os.environ.get(endpoint.api_key_env)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = Request(completion_url, data=payload, headers=headers, method="POST")
        try:
            with opener.open(request, timeout=MODEL_TIMEOUT_SECONDS) as response:
                raw = response.read()
        except HTTPError as error:
            raise ModelCallError(
                f"local model endpoint returned HTTP {error.code}"
            ) from error
        except (OSError, URLError) as error:
            raise ModelCallError(
                f"local model endpoint is unavailable: {error}"
            ) from error
        try:
            decoded: object = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ModelCallError(
                "local model endpoint returned invalid JSON"
            ) from error
        if not isinstance(decoded, dict):
            raise ModelCallError("local model endpoint response must be an object")
        choices = decoded.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelCallError("local model endpoint response has no choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise ModelCallError("local model endpoint returned an invalid choice")
        message = first.get("message")
        if not isinstance(message, dict):
            raise ModelCallError("local model endpoint choice has no message")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ModelCallError("local model endpoint returned empty content")
        return content

    return call


def fixture_model_callable(path: Path) -> Callable[[str, str], str]:
    try:
        raw: object = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ModelCallError(f"cannot read memory fixture {path}: {error}") from error
    if not isinstance(raw, dict) or set(raw) != {"judge", "distill"}:
        raise ModelCallError("memory fixture must contain exactly judge and distill")
    values = cast(dict[str, object], raw)
    judge = values["judge"]
    distill = values["distill"]
    if not isinstance(judge, str) or not judge.strip():
        raise ModelCallError("memory fixture judge must be a nonempty string")
    if not isinstance(distill, str) or not distill.strip():
        raise ModelCallError("memory fixture distill must be a nonempty string")

    def call(system_prompt: str, _prompt: str) -> str:
        if system_prompt == AUTOEVAL_TRAJECTORY_JUDGE_SYSTEM_PROMPT:
            return judge
        return distill

    return call


def configured_model_callable(
    settings: SynthSettings,
    fixture_path: Path | None = None,
) -> Callable[[str, str], str]:
    if fixture_path is not None:
        return fixture_model_callable(fixture_path.resolve())
    return local_model_callable(settings)


def fixture_path_from_environment() -> Path | None:
    configured = os.environ.get(FIXTURE_ENV)
    return Path(configured).expanduser().resolve() if configured else None
