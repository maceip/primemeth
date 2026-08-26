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
    META_HEURISTIC_CONTRAST_SI,
)
from synth.config import EndpointApi, SynthSettings

MODEL_TIMEOUT_SECONDS = 60.0
FIXTURE_ENV = "SYNTH_MEMORY_FIXTURE"


class ModelCallError(RuntimeError):
    """Raised when the configured local model cannot return a valid response."""


def _model_url(base_url: str, api: EndpointApi) -> str:
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
    suffix = "responses" if api == "openai-responses" else "chat/completions"
    return f"{base_url.rstrip('/')}/{suffix}"


def _request_payload(
    api: EndpointApi,
    *,
    model: str,
    system_prompt: str,
    prompt: str,
) -> dict[str, object]:
    if api == "openai-responses":
        return {
            "model": model,
            "instructions": system_prompt,
            "input": prompt,
            "stream": False,
            "store": False,
        }
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "stream": False,
    }


def _response_text(decoded: object, api: EndpointApi) -> str:
    if not isinstance(decoded, dict):
        raise ModelCallError("model endpoint response must be an object")
    if api == "openai-completions":
        choices = decoded.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelCallError("model endpoint response has no choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise ModelCallError("model endpoint returned an invalid choice")
        message = first.get("message")
        if not isinstance(message, dict):
            raise ModelCallError("model endpoint choice has no message")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ModelCallError("model endpoint returned empty content")
        return content

    status = decoded.get("status")
    if status is not None and status != "completed":
        detail: object = status
        error = decoded.get("error")
        incomplete = decoded.get("incomplete_details")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            detail = error["message"]
        elif isinstance(incomplete, dict) and isinstance(incomplete.get("reason"), str):
            detail = incomplete["reason"]
        raise ModelCallError(f"model endpoint response did not complete: {detail}")
    output = decoded.get("output")
    texts: list[str] = []
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "output_text":
                    continue
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text)
    rendered = "".join(texts)
    output_text = decoded.get("output_text")
    if not rendered.strip() and isinstance(output_text, str):
        rendered = output_text
    if not rendered.strip():
        raise ModelCallError("model endpoint returned empty output text")
    return rendered


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


def local_model_callable(
    settings: SynthSettings,
    *,
    model_id: str | None = None,
) -> Callable[[str, str], str]:
    endpoint = settings.endpoint
    if endpoint is None:
        raise ModelCallError("endpoint is required for ReasoningBank model calls")
    model_url = _model_url(endpoint.base_url, endpoint.api)
    model = model_id or _model_id(settings)
    opener = build_opener(ProxyHandler({}))

    def call(system_prompt: str, prompt: str) -> str:
        payload = json.dumps(
            _request_payload(
                endpoint.api,
                model=model,
                system_prompt=system_prompt,
                prompt=prompt,
            )
        ).encode()
        headers = {"Content-Type": "application/json"}
        api_key = os.environ.get(endpoint.api_key_env)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = Request(model_url, data=payload, headers=headers, method="POST")
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
        return _response_text(decoded, endpoint.api)

    return call


def fixture_model_callable(path: Path) -> Callable[[str, str], str]:
    try:
        raw: object = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ModelCallError(f"cannot read memory fixture {path}: {error}") from error
    if not isinstance(raw, dict) or set(raw) not in (
        {"judge", "distill"},
        {"judge", "distill", "meta"},
    ):
        raise ModelCallError(
            "memory fixture must contain judge and distill, with optional meta"
        )
    values = cast(dict[str, object], raw)
    judge = values["judge"]
    distill = values["distill"]
    meta = values.get("meta", distill)
    if not isinstance(judge, str) or not judge.strip():
        raise ModelCallError("memory fixture judge must be a nonempty string")
    if not isinstance(distill, str) or not distill.strip():
        raise ModelCallError("memory fixture distill must be a nonempty string")
    if not isinstance(meta, str) or not meta.strip():
        raise ModelCallError("memory fixture meta must be a nonempty string")

    def call(system_prompt: str, _prompt: str) -> str:
        if system_prompt == AUTOEVAL_TRAJECTORY_JUDGE_SYSTEM_PROMPT:
            return judge
        if system_prompt == META_HEURISTIC_CONTRAST_SI:
            return meta
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
