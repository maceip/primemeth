from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from synth.config import SynthConfigError, load_synth_settings
from synth.model import ModelCallError, _response_text, local_model_callable


def endpoint_config(base_url: str, *, api: str | None = None) -> dict[str, object]:
    endpoint: dict[str, object] = {
        "baseUrl": base_url,
        "model": "fixture-main",
        "smallModel": "fixture-small",
        "apiKeyEnv": "SYNTH_TEST_API_KEY",
    }
    if api is not None:
        endpoint["api"] = api
    return {"endpoint": endpoint}


def test_endpoint_api_defaults_to_chat_and_rejects_unknown_protocol(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "synth.json"
    config_path.write_text(json.dumps(endpoint_config("http://localhost:8000/v1")))
    endpoint = load_synth_settings(tmp_path).endpoint
    assert endpoint is not None
    assert endpoint.api == "openai-completions"

    config_path.write_text(
        json.dumps(endpoint_config("http://localhost:8000/v1", api="openai-responses"))
    )
    responses_endpoint = load_synth_settings(tmp_path).endpoint
    assert responses_endpoint is not None
    assert responses_endpoint.api == "openai-responses"

    config_path.write_text(
        json.dumps(endpoint_config("http://localhost:8000/v1", api="unknown-protocol"))
    )
    with pytest.raises(SynthConfigError, match="endpoint.api"):
        load_synth_settings(tmp_path)


def test_responses_model_call_uses_response_items(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    received: dict[str, object] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received["path"] = self.path
            received["authorization"] = self.headers.get("Authorization")
            length = int(self.headers["Content-Length"])
            received["body"] = json.loads(self.rfile.read(length))
            response = json.dumps(
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Thoughts: Evidence passed.\nSta",
                                },
                                {"type": "output_text", "text": 'tus: "success"'},
                            ],
                        }
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        (tmp_path / "synth.json").write_text(
            json.dumps(
                endpoint_config(f"http://{host}:{port}/v1", api="openai-responses")
            )
        )
        monkeypatch.setenv("SYNTH_TEST_API_KEY", "fixture-secret")
        settings = load_synth_settings(tmp_path)
        result = local_model_callable(settings)("Judge carefully", "Inspect evidence")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result == 'Thoughts: Evidence passed.\nStatus: "success"'
    assert received["path"] == "/v1/responses"
    assert received["authorization"] == "Bearer fixture-secret"
    body = received["body"]
    assert isinstance(body, dict)
    assert body == {
        "model": "fixture-small",
        "instructions": "Judge carefully",
        "input": "Inspect evidence",
        "stream": False,
        "store": False,
    }


def test_chat_completions_model_call_remains_supported(
    tmp_path: Path,
) -> None:
    received: dict[str, object] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received["path"] = self.path
            length = int(self.headers["Content-Length"])
            received["body"] = json.loads(self.rfile.read(length))
            response = json.dumps(
                {"choices": [{"message": {"content": "legacy result"}}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        (tmp_path / "synth.json").write_text(
            json.dumps(endpoint_config(f"http://{host}:{port}/v1"))
        )
        result = local_model_callable(load_synth_settings(tmp_path))(
            "Legacy system", "Legacy prompt"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result == "legacy result"
    assert received["path"] == "/v1/chat/completions"
    assert received["body"] == {
        "model": "fixture-small",
        "messages": [
            {"role": "system", "content": "Legacy system"},
            {"role": "user", "content": "Legacy prompt"},
        ],
        "temperature": 0,
        "stream": False,
    }


def test_responses_model_call_rejects_nonterminal_and_empty_output() -> None:
    with pytest.raises(ModelCallError, match="quota fixture"):
        _response_text(
            {
                "status": "failed",
                "error": {"code": "fixture", "message": "quota fixture"},
            },
            "openai-responses",
        )
    with pytest.raises(ModelCallError, match="max_output_tokens"):
        _response_text(
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
            },
            "openai-responses",
        )
    with pytest.raises(ModelCallError, match="empty output"):
        _response_text(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "refusal", "refusal": "no"}],
                    }
                ],
            },
            "openai-responses",
        )
