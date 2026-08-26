"""OpenAI-compatible context-depth and decode-throughput benchmark harness."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import re
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from tokenizers import Tokenizer

CONTEXT_LADDER = (4_096, 32_768, 131_072, 262_144)
ONE_M_CONTEXT = 1_048_576
DEFAULT_ENDPOINT = "http://127.0.0.1:8000/v1"
DEFAULT_OUTPUT_TOKENS = 192
MIN_DECODE_TOKENS = 128
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class BenchmarkError(RuntimeError):
    """Raised when the benchmark itself cannot be prepared or persisted."""


@dataclass(frozen=True, slots=True)
class BenchmarkRequest:
    endpoint: str = DEFAULT_ENDPOINT
    model: str | None = None
    engine: str = "openai-compatible"
    quant: str = "unknown"
    contexts: tuple[int, ...] = CONTEXT_LADDER
    try_1m: bool = False
    runs: int = 2
    output_tokens: int = DEFAULT_OUTPUT_TOKENS
    timeout_seconds: float = 1_800.0
    seed: int = 38
    chars_per_token: float = 3.2
    compare_mtp: bool = False
    mtp_depth: int = 3
    api_key_env: str = "SYNTH_LOCAL_API_KEY"
    tokenizer: Path | None = None
    run_id: str | None = None


def parse_contexts(value: str | None) -> tuple[int, ...]:
    if value is None:
        return CONTEXT_LADDER
    aliases = {
        "4k": 4_096,
        "32k": 32_768,
        "131k": 131_072,
        "262k": 262_144,
        "1m": ONE_M_CONTEXT,
    }
    contexts: list[int] = []
    for raw in value.split(","):
        token = raw.strip().casefold()
        if not token:
            continue
        if token in aliases:
            context = aliases[token]
        else:
            try:
                context = int(token.replace("_", ""))
            except ValueError as error:
                raise BenchmarkError(f"invalid context rung: {raw!r}") from error
        if context not in (*CONTEXT_LADDER, ONE_M_CONTEXT):
            allowed = ", ".join(("4k", "32k", "131k", "262k", "1m"))
            raise BenchmarkError(f"context rung must be one of {allowed}: {raw!r}")
        if context not in contexts:
            contexts.append(context)
    if not contexts:
        raise BenchmarkError("--contexts must select at least one rung")
    return tuple(contexts)


def default_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"bench-{stamp}-{os.getpid()}"


def _validate_request(request: BenchmarkRequest) -> None:
    if request.runs < 1:
        raise BenchmarkError("--runs must be at least 1")
    if request.output_tokens < MIN_DECODE_TOKENS:
        raise BenchmarkError(f"--output-tokens must be at least {MIN_DECODE_TOKENS}")
    if request.timeout_seconds <= 0:
        raise BenchmarkError("--timeout-seconds must be positive")
    if request.chars_per_token <= 0:
        raise BenchmarkError("--chars-per-token must be positive")
    if request.compare_mtp and request.engine != "mtplx":
        raise BenchmarkError("--compare-mtp requires --engine mtplx")
    if not 1 <= request.mtp_depth <= 8:
        raise BenchmarkError("--mtp-depth must be between 1 and 8")
    if request.run_id is not None and _RUN_ID_PATTERN.fullmatch(request.run_id) is None:
        raise BenchmarkError(
            "--run-id must contain only letters, digits, dot, dash, or underscore"
        )


def _normalized_endpoint(endpoint: str) -> str:
    normalized = endpoint.strip().rstrip("/")
    if not normalized.startswith(("http://", "https://")):
        raise BenchmarkError("--endpoint must be an HTTP(S) URL")
    return normalized if normalized.endswith("/v1") else normalized + "/v1"


def _headers(api_key_env: str) -> dict[str, str]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    api_key = os.environ.get(api_key_env)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _json_request(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, object] | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, object]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        url,
        data=data,
        headers=headers,
        method="POST" if payload is not None else "GET",
    )
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            raw = response.read()
    except HTTPError as error:
        detail = error.read().decode(errors="replace")[:1_000]
        raise BenchmarkError(f"{url} returned HTTP {error.code}: {detail}") from error
    except (OSError, URLError) as error:
        raise BenchmarkError(f"{url} is unavailable: {error}") from error
    try:
        decoded: object = json.loads(raw)
    except json.JSONDecodeError as error:
        raise BenchmarkError(f"{url} returned invalid JSON") from error
    if not isinstance(decoded, dict) or not all(
        isinstance(key, str) for key in decoded
    ):
        raise BenchmarkError(f"{url} returned a non-object JSON response")
    return cast(dict[str, object], decoded)


def _discover_model(endpoint: str, headers: dict[str, str]) -> str:
    response = _json_request(endpoint + "/models", headers=headers)
    data = response.get("data")
    if not isinstance(data, list):
        raise BenchmarkError("/v1/models returned no model list")
    for item in data:
        if isinstance(item, dict):
            model_id = item.get("id")
            if isinstance(model_id, str) and model_id:
                return model_id
    raise BenchmarkError("/v1/models returned no usable model id")


def _machine() -> str:
    fields = [platform.machine()]
    try:
        completed = subprocess.run(
            ["sysctl", "-n", "hw.model", "machdep.cpu.brand_string", "hw.memsize"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        completed = None
    if completed is not None and completed.returncode == 0:
        values = [
            line.strip() for line in completed.stdout.splitlines() if line.strip()
        ]
        if values:
            fields.insert(0, values[0])
        if len(values) > 1 and values[1] not in fields:
            fields.append(values[1])
        if len(values) > 2:
            with suppress(ValueError):
                fields.append(f"{int(values[2]) / (1024**3):.0f} GiB")
    return " / ".join(fields)


def _module_source(
    module_id: int,
    next_id: int | None,
    weight: int,
    route_token: int,
    feature: str,
) -> str:
    next_value = "None" if next_id is None else f'"service_{next_id:05d}"'
    return (
        f"# FILE: src/services/service_{module_id:05d}.py\n"
        '"""Generated service node used by the repository routing audit."""\n\n'
        f'SERVICE_ID = "service_{module_id:05d}"\n'
        f"NEXT_SERVICE = {next_value}\n"
        f"ROUTE_WEIGHT = {weight}\n"
        f"ROUTE_TOKEN = 0x{route_token:08x}\n"
        f'FEATURE_FLAG = "{feature}"\n\n'
        "def forward(payload: dict[str, object]) -> tuple[str | None, int]:\n"
        '    """Return the next hop and add this service\'s audit weight."""\n'
        '    current = int(payload.get("route_weight", 0))\n'
        '    payload["route_weight"] = current + ROUTE_WEIGHT\n'
        '    payload.setdefault("features", []).append(FEATURE_FLAG)\n'
        "    return NEXT_SERVICE, ROUTE_TOKEN\n\n"
    )


def _coding_prompt(
    target_tokens: int, *, seed: int, chars_per_token: float
) -> tuple[str, dict[str, object]]:
    target_chars = max(8_000, int(target_tokens * chars_per_token))
    rng = random.Random(seed + target_tokens)
    module_count = max(12, target_chars // 580)
    chain = list(range(module_count))
    rng.shuffle(chain)
    next_by_id: dict[int, int | None] = {
        module_id: chain[index + 1] if index + 1 < len(chain) else None
        for index, module_id in enumerate(chain)
    }
    weights = {module_id: rng.randint(1, 997) for module_id in chain}
    route_tokens = {module_id: rng.getrandbits(32) for module_id in chain}
    feature_kinds = ("cache", "parser", "queue", "retry")
    features = {
        module_id: f"flag_{rng.choice(feature_kinds)}_{module_id:05d}"
        for module_id in chain
    }
    dump_order = list(chain)
    rng.shuffle(dump_order)
    prefix = (
        "You are reviewing a synthetic but internally consistent Python repository.\n"
        "Treat every file as source of truth; do not assume file order is "
        "route order.\n"
        "Follow references exactly and finish with the requested patch.\n\n"
        "=== REPOSITORY DUMP ===\n\n"
    )
    body = "".join(
        _module_source(
            module_id,
            next_by_id[module_id],
            weights[module_id],
            route_tokens[module_id],
            features[module_id],
        )
        for module_id in dump_order
    )
    tail = (
        "\n=== CODING TASK ===\n"
        f"The production entry point is service_{chain[0]:05d}. Follow NEXT_SERVICE "
        "until None. Compute the sum of ROUTE_WEIGHT and the XOR of every "
        "ROUTE_TOKEN on that path. Identify the terminal service and the first "
        "feature flag containing 'retry'. Also report the number and sum of "
        "shard_weight values in every AUDIT EVENT line. Then provide a Python "
        "patch for "
        "src/route_audit.py implementing audit_route(registry, entry) without "
        "recursion, including cycle and missing-service errors. Explain the "
        "cross-file invariants and include enough detail that the response uses "
        f"at least {MIN_DECODE_TOKENS} tokens.\n"
    )
    prompt = prefix + body
    audit_sum = 0
    audit_count = 0
    while len(prompt) + len(tail) < target_chars - 80:
        value = rng.randint(1, 9_999)
        line = f"# AUDIT EVENT {audit_count:07d}: shard_weight={value:04d}\n"
        if len(prompt) + len(line) + len(tail) > target_chars:
            break
        prompt += line
        audit_sum += value
        audit_count += 1
    remaining = target_chars - len(prompt) - len(tail)
    if remaining > 2:
        prompt += "#" + (" audit" * ((remaining - 1) // 6 + 1))[: remaining - 1]
    prompt += tail
    route_xor = 0
    for module_id in chain:
        route_xor ^= route_tokens[module_id]
    fixture = {
        "targetTokens": target_tokens,
        "targetChars": target_chars,
        "promptChars": len(prompt),
        "promptSha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "moduleCount": module_count,
        "entryService": f"service_{chain[0]:05d}",
        "terminalService": f"service_{chain[-1]:05d}",
        "routeWeightSum": sum(weights.values()),
        "routeTokenXor": f"0x{route_xor:08x}",
        "auditEventCount": audit_count,
        "auditEventWeightSum": audit_sum,
    }
    return prompt, fixture


def _tokenizer(request: BenchmarkRequest, model: str) -> Tokenizer | None:
    configured = request.tokenizer
    if configured is None:
        model_path = Path(model).expanduser()
        if model_path.is_dir():
            configured = model_path / "tokenizer.json"
    if configured is None:
        return None
    path = configured.expanduser().resolve()
    if path.is_dir():
        path = path / "tokenizer.json"
    if not path.is_file():
        raise BenchmarkError(f"tokenizer.json is unavailable: {path}")
    try:
        return Tokenizer.from_file(str(path))
    except Exception as error:
        raise BenchmarkError(f"cannot load tokenizer {path}: {error}") from error


def _fitted_prompt(
    context: int, *, request: BenchmarkRequest, tokenizer: Tokenizer | None
) -> tuple[str, dict[str, object]]:
    chars_per_token = request.chars_per_token
    prompt, fixture = _coding_prompt(
        context,
        seed=request.seed,
        chars_per_token=chars_per_token,
    )
    if tokenizer is None:
        return prompt, fixture
    for _ in range(3):
        measured = len(tokenizer.encode(prompt, add_special_tokens=False).ids)
        if abs(measured - context) <= max(16, context // 200):
            break
        chars_per_token *= context / max(1, measured)
        prompt, fixture = _coding_prompt(
            context,
            seed=request.seed,
            chars_per_token=chars_per_token,
        )
    prompt_tokens = len(tokenizer.encode(prompt, add_special_tokens=False).ids)
    fixture["localTokenizerPromptTokens"] = prompt_tokens
    fixture["fittedCharsPerToken"] = chars_per_token
    return prompt, fixture


def _usage_tokens(value: object, key: str) -> int | None:
    if not isinstance(value, dict):
        return None
    token_value = value.get(key)
    return token_value if isinstance(token_value, int) else None


def _chunk_text(decoded: dict[str, object]) -> str:
    choices = decoded.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0]
    if not isinstance(choice, dict):
        return ""
    delta = choice.get("delta")
    if not isinstance(delta, dict):
        return ""
    for key in ("content", "reasoning_content", "reasoning"):
        value = delta.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _stream_completion(
    endpoint: str,
    *,
    headers: dict[str, str],
    model: str,
    prompt: str,
    output_tokens: int,
    seed: int,
    timeout_seconds: float,
    tokenizer: Tokenizer | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": output_tokens,
        "temperature": 0,
        "seed": seed,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    request = Request(
        endpoint + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={**headers, "Accept": "text/event-stream"},
        method="POST",
    )
    opener = build_opener(ProxyHandler({}))
    started = time.monotonic()
    first_token_at: float | None = None
    completion_tokens: int | None = None
    prompt_tokens: int | None = None
    received_chars = 0
    output_parts: list[str] = []
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            for raw_line in response:
                if time.monotonic() - started > timeout_seconds:
                    raise TimeoutError(
                        f"completion exceeded {timeout_seconds:g}s wall timeout"
                    )
                line = raw_line.decode(errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    decoded: object = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(decoded, dict) or not all(
                    isinstance(key, str) for key in decoded
                ):
                    continue
                chunk = cast(dict[str, object], decoded)
                text = _chunk_text(chunk)
                if text:
                    if first_token_at is None:
                        first_token_at = time.monotonic()
                    received_chars += len(text)
                    output_parts.append(text)
                usage = chunk.get("usage")
                completion_tokens = (
                    _usage_tokens(usage, "completion_tokens") or completion_tokens
                )
                prompt_tokens = _usage_tokens(usage, "prompt_tokens") or prompt_tokens
    except HTTPError as error:
        detail = error.read().decode(errors="replace")[:2_000]
        raise BenchmarkError(
            f"chat completion returned HTTP {error.code}: {detail}"
        ) from error
    except (OSError, URLError, TimeoutError) as error:
        raise BenchmarkError(f"chat completion failed: {error}") from error
    finished = time.monotonic()
    if first_token_at is None:
        raise BenchmarkError("stream completed without a generated token")
    if completion_tokens is None:
        if tokenizer is None:
            raise BenchmarkError(
                "stream returned no completion_tokens usage and no local tokenizer "
                "was available; pass --tokenizer"
            )
        completion_tokens = len(
            tokenizer.encode("".join(output_parts), add_special_tokens=False).ids
        )
        completion_token_source = "local_tokenizer"
    else:
        completion_token_source = "api_usage"
    if prompt_tokens is None:
        prompt_tokens = (
            len(tokenizer.encode(prompt, add_special_tokens=False).ids)
            if tokenizer is not None
            else None
        )
    if completion_tokens < MIN_DECODE_TOKENS:
        raise BenchmarkError(
            f"short generation: {completion_tokens} tokens; need at least "
            f"{MIN_DECODE_TOKENS}"
        )
    decode_seconds = finished - first_token_at
    if decode_seconds <= 0:
        raise BenchmarkError("decode duration was not measurable")
    return {
        "promptTokens": prompt_tokens,
        "completionTokens": completion_tokens,
        "completionTokenSource": completion_token_source,
        "receivedChars": received_chars,
        "timeToFirstTokenSeconds": first_token_at - started,
        "decodeSeconds": decode_seconds,
        "decodeTokensPerSecond": max(0, completion_tokens - 1) / decode_seconds,
        "totalWallSeconds": finished - started,
    }


def _settings_url(endpoint: str) -> str:
    return endpoint + "/mtplx/settings"


def _mtplx_settings(
    endpoint: str, headers: dict[str, str], update: dict[str, object] | None = None
) -> dict[str, object]:
    return _json_request(
        _settings_url(endpoint),
        headers=headers,
        payload=update,
        timeout_seconds=15.0,
    )


def _base_row(
    *,
    model: str,
    request: BenchmarkRequest,
    machine: str,
    context: int,
    mode: str,
    run: int,
    fixture: dict[str, object],
) -> dict[str, object]:
    return {
        "model": model,
        "engine": request.engine,
        "quant": request.quant,
        "machine": machine,
        "contextLength": context,
        "contextProfile": "yarn-1m" if context == ONE_M_CONTEXT else "native",
        "mode": mode,
        "draftDepth": request.mtp_depth
        if mode == "mtp"
        else 0
        if mode == "ar"
        else None,
        "run": run,
        "fixture": fixture,
    }


def _best_rows(attempts: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[tuple[int, str], list[dict[str, object]]] = {}
    for row in attempts:
        context = row.get("contextLength")
        mode = row.get("mode")
        if isinstance(context, int) and isinstance(mode, str):
            grouped.setdefault((context, mode), []).append(row)
    best: list[dict[str, object]] = []
    for key in sorted(grouped, key=lambda item: (item[0], item[1])):
        rows = grouped[key]
        successful = [
            row
            for row in rows
            if row.get("status") == "ok"
            and isinstance(row.get("decodeTokensPerSecond"), (int, float))
        ]
        if successful:
            selected = max(
                successful,
                key=lambda row: float(cast(float, row["decodeTokensPerSecond"])),
            )
        else:
            selected = rows[0]
        best.append(dict(selected))
    by_context_mode = {
        (cast(int, row["contextLength"]), cast(str, row["mode"])): row for row in best
    }
    for row in best:
        context = cast(int, row["contextLength"])
        if row.get("mode") != "mtp" or row.get("status") != "ok":
            continue
        baseline = by_context_mode.get((context, "ar"))
        if baseline is None or baseline.get("status") != "ok":
            continue
        baseline_tps = baseline.get("decodeTokensPerSecond")
        current_tps = row.get("decodeTokensPerSecond")
        if (
            isinstance(baseline_tps, (int, float))
            and isinstance(current_tps, (int, float))
            and baseline_tps > 0
        ):
            row["mtpSpeedup"] = float(current_tps) / float(baseline_tps)
            baseline["mtpSpeedup"] = 1.0
    return best


def _display_number(value: object, suffix: str = "") -> str:
    return f"{float(value):.2f}{suffix}" if isinstance(value, (int, float)) else "—"


def render_markdown(report: dict[str, object]) -> str:
    rows = report.get("rows")
    rendered = [
        f"# Synth benchmark {report.get('id', '')}",
        "",
        "| Status | Model | Engine | Quant | Machine | Context | Mode | TTFT | "
        "Decode | Wall | Output | MTP speedup |",
        "|---|---|---|---|---|---:|---|---:|---:|---:|---:|---:|",
    ]
    if isinstance(rows, list):
        for value in rows:
            if not isinstance(value, dict):
                continue
            row = cast(dict[str, object], value)
            status = str(row.get("status", "error"))
            context = row.get("promptTokens") or row.get("contextLength")
            if status != "ok":
                status = (
                    "FAIL: "
                    + str(row.get("error", "unknown error")).replace("|", "\\|")[:120]
                )
            rendered.append(
                "| "
                + " | ".join(
                    (
                        status,
                        str(row.get("model", "")),
                        str(row.get("engine", "")),
                        str(row.get("quant", "")),
                        str(row.get("machine", "")).replace("|", "\\|"),
                        str(context),
                        str(row.get("mode", "server")),
                        _display_number(row.get("timeToFirstTokenSeconds"), "s"),
                        _display_number(row.get("decodeTokensPerSecond"), " tok/s"),
                        _display_number(row.get("totalWallSeconds"), "s"),
                        str(row.get("completionTokens", "—")),
                        _display_number(row.get("mtpSpeedup"), "x"),
                    )
                )
                + " |"
            )
    rendered.extend(
        (
            "",
            "Context reports measured API prompt tokens when the endpoint supplies "
            "usage; "
            "otherwise a failed row is recorded.",
            "",
        )
    )
    return "\n".join(rendered)


def run_benchmark(workspace: Path, request: BenchmarkRequest) -> dict[str, object]:
    _validate_request(request)
    endpoint = _normalized_endpoint(request.endpoint)
    headers = _headers(request.api_key_env)
    model = request.model or _discover_model(endpoint, headers)
    tokenizer = _tokenizer(request, model)
    run_id = request.run_id or default_run_id()
    if _RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise BenchmarkError(f"invalid benchmark run id: {run_id!r}")
    contexts = list(request.contexts)
    if request.try_1m and ONE_M_CONTEXT not in contexts:
        contexts.append(ONE_M_CONTEXT)
    machine = _machine()
    attempts: list[dict[str, object]] = []
    modes = ("ar", "mtp") if request.compare_mtp else ("server",)
    original_settings: dict[str, object] | None = None
    restore_error: str | None = None
    if request.compare_mtp:
        original_settings = _mtplx_settings(endpoint, headers)
    try:
        for context in contexts:
            prompt, fixture = _fitted_prompt(
                context,
                request=request,
                tokenizer=tokenizer,
            )
            for mode in modes:
                setting_error: str | None = None
                if request.compare_mtp:
                    update: dict[str, object] = {"generation_mode": mode}
                    if mode == "mtp":
                        update["depth"] = request.mtp_depth
                    try:
                        _mtplx_settings(endpoint, headers, update)
                    except BenchmarkError as error:
                        setting_error = str(error)
                for run in range(1, request.runs + 1):
                    row = _base_row(
                        model=model,
                        request=request,
                        machine=machine,
                        context=context,
                        mode=mode,
                        run=run,
                        fixture=fixture,
                    )
                    if setting_error is not None:
                        row.update({"status": "error", "error": setting_error})
                        attempts.append(row)
                        continue
                    try:
                        measured = _stream_completion(
                            endpoint,
                            headers=headers,
                            model=model,
                            prompt=prompt,
                            output_tokens=request.output_tokens,
                            seed=request.seed + run - 1,
                            timeout_seconds=request.timeout_seconds,
                            tokenizer=tokenizer,
                        )
                        row.update({"status": "ok", **measured})
                    except BenchmarkError as error:
                        row.update({"status": "error", "error": str(error)})
                    attempts.append(row)
    finally:
        if original_settings is not None:
            generation_mode = original_settings.get("generation_mode")
            depth = original_settings.get("depth")
            restore: dict[str, object] = {}
            if isinstance(generation_mode, str):
                restore["generation_mode"] = generation_mode
            if isinstance(depth, int):
                restore["depth"] = depth
            if restore:
                try:
                    _mtplx_settings(endpoint, headers, restore)
                except BenchmarkError as error:
                    restore_error = str(error)
    rows = _best_rows(attempts)
    complete = restore_error is None and all(row.get("status") == "ok" for row in rows)
    report: dict[str, object] = {
        "schemaVersion": 1,
        "id": run_id,
        "generatedAt": datetime.now(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "endpoint": endpoint,
        "model": model,
        "engine": request.engine,
        "quant": request.quant,
        "machine": machine,
        "complete": complete,
        "restoreError": restore_error,
        "selection": "best decodeTokensPerSecond per context and mode",
        "config": {
            "contexts": contexts,
            "runs": request.runs,
            "outputTokens": request.output_tokens,
            "minimumDecodeTokens": MIN_DECODE_TOKENS,
            "timeoutSeconds": request.timeout_seconds,
            "seed": request.seed,
            "charsPerToken": request.chars_per_token,
            "compareMtp": request.compare_mtp,
            "mtpDepth": request.mtp_depth if request.compare_mtp else None,
        },
        "rows": rows,
        "attempts": attempts,
    }
    output = workspace.expanduser().resolve() / ".synth" / "bench" / run_id
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    report_path = output / "report.json"
    markdown_path = output / "report.md"
    report["reportPath"] = str(report_path)
    report["markdownPath"] = str(markdown_path)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    markdown_path.write_text(render_markdown(report))
    return report
