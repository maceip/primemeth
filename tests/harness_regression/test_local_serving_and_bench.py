from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from synth import bench as bench_module
from synth import serve as serve_module
from synth.attribution import (
    MTPLX_BENCHMARK_CREDIT,
    MTPLX_PRODUCT_ATTRIBUTION,
    MTPLX_URL,
)
from synth.bench import (
    CHAT_TEMPLATE_RESERVE_TOKENS,
    BenchmarkError,
    BenchmarkRequest,
    _fitted_prompt,
    _json_request,
    _normalized_endpoint,
    run_benchmark,
)
from synth.cli import _normalize_serve_shorthand, _parser
from synth.engine import EngineError, resolve_llama_server
from synth.serve import (
    ServeError,
    _gguf_model,
    _mtplx_argv,
    _resolve_mtplx_checkout,
    start,
)
from synth.serving import ManagedServeRequest


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://localhost:8000", "http://localhost:8000/v1"),
        ("http://127.0.0.1:8080/v1/", "http://127.0.0.1:8080/v1"),
        ("http://[::1]:9000/api", "http://[::1]:9000/api/v1"),
    ],
)
def test_benchmark_endpoint_accepts_only_normalized_loopback(
    value: str, expected: str
) -> None:
    assert _normalized_endpoint(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "https://127.0.0.1:8000/v1",
        "http://example.com/v1",
        "http://10.0.0.12:8000/v1",
        "http://localhost.evil.example/v1",
        "http://localhost:8000@evil.example/v1",
        "http://user:secret@localhost:8000/v1",
        "http://localhost:8000/v1?redirect=https://example.com",
        "http://localhost:8000/v1#fragment",
        "http://localhost:99999/v1",
    ],
)
def test_benchmark_endpoint_rejects_remote_or_ambiguous_urls(value: str) -> None:
    with pytest.raises(BenchmarkError, match="--endpoint"):
        _normalized_endpoint(value)


def test_benchmark_http_client_never_follows_redirects() -> None:
    target_requests: list[str | None] = []

    class TargetHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            target_requests.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, _format: str, *_args: object) -> None:
            return

    target_server = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
    target_thread = Thread(target=target_server.serve_forever, daemon=True)
    target_thread.start()
    target_host, target_port = target_server.server_address

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header(
                "Location", f"http://{target_host}:{target_port}/forbidden-target"
            )
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    redirect_server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    redirect_thread = Thread(target=redirect_server.serve_forever, daemon=True)
    redirect_thread.start()
    redirect_host, redirect_port = redirect_server.server_address
    try:
        with pytest.raises(BenchmarkError, match="returned HTTP 302"):
            _json_request(
                f"http://{redirect_host}:{redirect_port}/v1/models",
                headers={"Authorization": "Bearer fixture-secret"},
            )
    finally:
        redirect_server.shutdown()
        redirect_server.server_close()
        redirect_thread.join(timeout=5)
        target_server.shutdown()
        target_server.server_close()
        target_thread.join(timeout=5)

    assert target_requests == []


def test_serve_shorthand_preserves_all_explicit_lifecycle_commands() -> None:
    explicit = (
        ["serve", "start", "--engine", "mtplx", "--model", "/tmp/model"],
        ["serve", "status", "--engine", "mtplx"],
        ["serve", "stop", "--engine", "mtplx"],
    )
    for arguments in explicit:
        normalized = _normalize_serve_shorthand(arguments)
        assert normalized == arguments
        parsed = _parser().parse_args(normalized)
        assert parsed.serve_command == arguments[1]

    shorthand = ["serve", "--engine", "mtplx", "--model", "/tmp/model"]
    normalized_shorthand = _normalize_serve_shorthand(shorthand)
    assert normalized_shorthand == [
        "serve",
        "start",
        "--engine",
        "mtplx",
        "--model",
        "/tmp/model",
    ]
    assert _parser().parse_args(normalized_shorthand).serve_command == "start"


def test_mtplx_serve_help_displays_required_attribution(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        _parser().parse_args(["serve", "start", "--help"])
    assert raised.value.code == 0
    assert MTPLX_PRODUCT_ATTRIBUTION in capsys.readouterr().out


def test_llama_server_custom_binary_must_be_explicit_and_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = tmp_path / "llama-server"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    monkeypatch.setenv("SYNTH_LLAMA_SERVER", str(executable))

    assert resolve_llama_server() == [str(executable.resolve())]

    monkeypatch.setenv("SYNTH_LLAMA_SERVER", "relative/llama-server")
    with pytest.raises(EngineError, match="absolute path"):
        resolve_llama_server()

    nonexecutable = tmp_path / "not-executable"
    nonexecutable.write_text("fixture")
    monkeypatch.setenv("SYNTH_LLAMA_SERVER", str(nonexecutable))
    with pytest.raises(EngineError, match="not an executable file"):
        resolve_llama_server()


def test_gguf_directory_requires_a_complete_multipart_set(tmp_path: Path) -> None:
    model = tmp_path / "model"
    model.mkdir()
    first = model / "qwen-00001-of-00002.gguf"
    second = model / "qwen-00002-of-00002.gguf"
    first.write_bytes(b"one")

    with pytest.raises(ServeError, match="incomplete multipart"):
        _gguf_model(model)

    second.write_bytes(b"two")
    assert _gguf_model(model) == first


def test_gguf_directory_rejects_shards_with_mixed_prefixes(tmp_path: Path) -> None:
    model = tmp_path / "model"
    model.mkdir()
    (model / "alpha-00001-of-00002.gguf").write_bytes(b"one")
    (model / "beta-00002-of-00002.gguf").write_bytes(b"two")

    with pytest.raises(ServeError, match="mixed multipart GGUF prefixes"):
        _gguf_model(model)


def _prepared_mtplx_checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "MTPLX"
    (checkout / "mtplx").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    (checkout / "mtplx" / "cli.py").write_text("# fixture\n")
    return checkout


def test_mtplx_checkout_requires_preprovisioned_uv_environment(
    tmp_path: Path,
) -> None:
    checkout = _prepared_mtplx_checkout(tmp_path)
    with pytest.raises(ServeError, match="no prepared uv environment"):
        _resolve_mtplx_checkout(checkout)

    python = checkout / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o700)
    assert _resolve_mtplx_checkout(checkout) == checkout.resolve()


@pytest.mark.parametrize(
    ("depth", "mode", "argv_depth"),
    [(0, "ar", "1"), (3, "mtp", "3")],
)
def test_mtplx_argv_is_offline_no_sync_and_depth_aware(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    depth: int,
    mode: str,
    argv_depth: str,
) -> None:
    checkout = _prepared_mtplx_checkout(tmp_path)
    model = tmp_path / "model"
    model.mkdir()
    monkeypatch.setattr(serve_module.shutil, "which", lambda name: "/usr/bin/uv")

    command = _mtplx_argv(
        checkout,
        model,
        "127.0.0.1",
        8000,
        "fixture-model",
        depth,
    )

    assert command[:7] == [
        "/usr/bin/uv",
        "run",
        "--project",
        str(checkout),
        "--python",
        "3.12",
        "--frozen",
    ]
    assert "--offline" in command
    assert "--no-sync" in command
    assert "--no-python-downloads" in command
    assert command[command.index("--generation-mode") + 1] == mode
    assert command[command.index("--depth") + 1] == argv_depth


def test_mtplx_start_state_carries_product_attribution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checkout = _prepared_mtplx_checkout(tmp_path)
    python = checkout / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o700)
    model = tmp_path / "model"
    model.mkdir()

    def fake_start(_workspace: Path, request: ManagedServeRequest) -> dict[str, object]:
        return {"details": request.details, "environment": request.environment}

    monkeypatch.setattr(serve_module, "start_managed_server", fake_start)
    monkeypatch.setattr(serve_module.shutil, "which", lambda name: "/usr/bin/uv")

    result = start(
        tmp_path,
        engine="mtplx",
        model=model,
        mtplx_checkout=checkout,
        draft_depth=3,
    )

    details = result["details"]
    environment = result["environment"]
    assert isinstance(details, dict)
    assert details["attribution"] == MTPLX_PRODUCT_ATTRIBUTION
    assert isinstance(environment, dict)
    assert environment["UV_OFFLINE"] == "1"
    assert environment["UV_NO_SYNC"] == "1"
    assert environment["UV_PYTHON_DOWNLOADS"] == "never"
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"


def test_benchmark_reserves_output_restores_mtplx_and_credits_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    request = BenchmarkRequest(
        endpoint="http://127.0.0.1:8000/v1",
        model="fixture-model",
        engine="mtplx",
        contexts=(4_096,),
        runs=1,
        output_tokens=128,
        compare_mtp=True,
        mtp_depth=3,
        run_id="fixture-run",
    )
    prompt, fixture = _fitted_prompt(4_096, request=request)
    assert prompt
    assert fixture["targetTokens"] == 4_096 - 128 - CHAT_TEMPLATE_RESERVE_TOKENS

    settings_calls: list[dict[str, object] | None] = []
    active_mode = {"value": "ar"}

    def fake_settings(
        _endpoint: str,
        _headers: dict[str, str],
        update: dict[str, object] | None = None,
    ) -> dict[str, object]:
        settings_calls.append(dict(update) if update is not None else None)
        if update is None:
            return {"generation_mode": "ar", "depth": 1}
        mode = update.get("generation_mode")
        if isinstance(mode, str):
            active_mode["value"] = mode
        return dict(update)

    def fake_stream(
        _endpoint: str,
        *,
        headers: dict[str, str],
        model: str,
        prompt: str,
        output_tokens: int,
        seed: int,
        timeout_seconds: float,
    ) -> dict[str, object]:
        assert headers["Content-Type"] == "application/json"
        assert model == "fixture-model"
        assert prompt
        assert output_tokens == 128
        assert seed == 38
        assert timeout_seconds == 1_800.0
        rate = 15.0 if active_mode["value"] == "mtp" else 10.0
        return {
            "promptTokens": 3_700,
            "completionTokens": 128,
            "completionTokenSource": "api_usage",
            "receivedChars": 512,
            "timeToFirstTokenSeconds": 1.0,
            "decodeSeconds": 12.7,
            "decodeTokensPerSecond": rate,
            "totalWallSeconds": 13.7,
        }

    monkeypatch.setattr(bench_module, "_mtplx_settings", fake_settings)
    monkeypatch.setattr(bench_module, "_stream_completion", fake_stream)
    monkeypatch.setattr(bench_module, "_machine", lambda: "fixture-machine")

    report = run_benchmark(tmp_path, request)

    assert report["complete"] is True
    assert report["attribution"] == {
        "benchmarkCredit": MTPLX_BENCHMARK_CREDIT,
        "url": MTPLX_URL,
    }
    assert settings_calls == [
        None,
        {"generation_mode": "ar"},
        {"generation_mode": "mtp", "depth": 3},
        {"generation_mode": "ar", "depth": 1},
    ]
    rows = report["rows"]
    assert isinstance(rows, list)
    mtp_row = next(row for row in rows if row["mode"] == "mtp")
    assert mtp_row["mtpSpeedup"] == 1.5

    report_path = tmp_path / ".synth" / "bench" / "fixture-run" / "report.json"
    markdown_path = report_path.with_name("report.md")
    persisted = json.loads(report_path.read_text())
    assert persisted["attribution"] == report["attribution"]
    markdown = markdown_path.read_text()
    assert MTPLX_BENCHMARK_CREDIT in markdown
    assert MTPLX_URL in markdown
