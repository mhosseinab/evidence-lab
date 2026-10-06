"""Only explicit, allowlisted workflow metadata can leave through LangSmith."""
import asyncio
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from evidence_lab.config import ConfigError, load_config
from evidence_lab import langsmith_trace
from evidence_lab.domain import CallContext


def test_tracing_is_disabled_even_with_ambient_credentials(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "ambient-private-key")
    send = Mock()
    monkeypatch.setattr(langsmith_trace, "_send", send)
    config = load_config("configs/mock.yaml")
    asyncio.run(langsmith_trace.export_trace(config, "r1", [], {"status": "answered"}))
    send.assert_not_called()


def test_trace_records_exclude_private_content_and_invalid_stages():
    config = load_config("configs/mock.yaml")
    records = langsmith_trace.trace_records(config, "run1", [
        {"node": "verify", "status": "completed", "elapsed_seconds": 0.1,
         "draft": "PRIVATE DRAFT", "question": "PRIVATE QUESTION", "api_key": "PRIVATE KEY"},
        {"node": "PRIVATE NODE", "status": "failed", "elapsed_seconds": 1},
        {"node": "repair", "status": "completed", "elapsed_seconds": float("inf")},
    ], {"status": "answered", "answer": "PRIVATE ANSWER", "error": "PRIVATE ERROR"})
    serialized = json.dumps(records, default=str)
    assert "PRIVATE" not in serialized
    assert len(records) == 2
    assert records[1]["parent_run_id"] == records[0]["id"]
    assert records[0]["outputs"]["status"] == "answered"


def test_tracing_outage_does_not_change_answer_or_log_provider_error(monkeypatch, caplog):
    config = load_config("configs/mock.yaml")
    config.langsmith.enabled = True
    monkeypatch.setattr(langsmith_trace, "_send", Mock(side_effect=RuntimeError("PRIVATE KEY")))
    asyncio.run(langsmith_trace.export_trace(config, "run1", [], {"status": "answered"}))
    assert "PRIVATE KEY" not in caplog.text
    assert "unavailable" in caplog.text


def test_langsmith_environment_key_is_explicit_and_redacted(monkeypatch, tmp_path):
    data = yaml.safe_load(Path("configs/mock.yaml").read_text())
    data["langsmith"] = {"enabled": True, "api_key_env": "EVIDENCE_LAB_TRACE_KEY"}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError):
        load_config(path)
    monkeypatch.setenv("EVIDENCE_LAB_TRACE_KEY", "private-trace-key")
    config = load_config(path)
    key = config.langsmith.api_key
    assert key is not None and key.get_secret_value() == "private-trace-key"
    assert "private-trace-key" not in json.dumps(config.safe_dict())


def test_disabled_langsmith_redacts_url_credentials(tmp_path):
    data = yaml.safe_load(Path("configs/mock.yaml").read_text())
    data["langsmith"] = {"enabled": False, "api_url": "https://operator:PRIVATE@traces.test/?key=PRIVATE"}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    assert "PRIVATE" not in json.dumps(load_config(path).safe_dict())


def test_content_capture_is_opt_in_redacted_and_bounded():
    from pydantic import SecretStr

    config = load_config("configs/mock.yaml")
    config.langsmith.enabled = True
    config.langsmith.api_key = SecretStr("private-trace-key")
    ctx = CallContext.for_seconds("run1", "queries")
    payload = {"messages": [{"content": "document excerpt private-trace-key"}]}
    response = {"choices": [{"message": {"content": "invalid schema private-trace-key"}}],
                "api_key": "another-secret"}
    langsmith_trace.capture_provider_call(config, ctx, "generator", payload, response, "invalid_response", 0.1)
    assert not ctx.provider_traces
    config.langsmith.capture_content = True
    langsmith_trace.capture_provider_call(config, ctx, "generator", payload, response, "invalid_response", 0.1)
    records = langsmith_trace.trace_records(config, "run1", [], {"status": "failed"}, provider_calls=ctx.provider_traces)
    encoded = json.dumps(records, default=str)
    assert "document excerpt" in encoded and "invalid schema" in encoded
    assert "private-trace-key" not in encoded and "another-secret" not in encoded
    assert records[0]["error"] and records[-1]["error"]
    assert records[-1]["parent_run_id"] == records[0]["id"]
    assert not ctx.calls  # Content never enters persistent budget accounting.
    langsmith_trace.capture_provider_call(config, ctx, "generator", {"text": "x" * langsmith_trace.MAX_CONTENT_BYTES}, {}, "ok", 0.1)
    assert ctx.provider_traces[-1]["content_omitted"] is True
    assert ctx.provider_traces[-1]["content"] == {}


@pytest.mark.parametrize("capture_content", [False, True])
def test_real_sdk_exports_only_explicit_content_to_selected_endpoint(capture_content):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from pydantic import SecretStr

    batches = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{}')

        def do_POST(self):
            batches.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = load_config("configs/mock.yaml")
        config.langsmith.enabled = True
        config.langsmith.api_url = f"http://127.0.0.1:{server.server_port}"
        config.langsmith.api_key = SecretStr("test-trace-key")
        config.langsmith.capture_content = capture_content
        ctx = CallContext.for_seconds("run1", "queries")
        langsmith_trace.capture_provider_call(config, ctx, "generator", {"messages": ["REQUEST CONTENT"]},
                                             {"response": "RESPONSE CONTENT", "api_key": "PRIVATE KEY"},
                                             "invalid_response", 0.1)
        records = langsmith_trace.trace_records(config, "run1", [
            {"node": "verify", "status": "completed", "elapsed_seconds": 0.1,
             "question": "PRIVATE QUESTION", "draft": "PRIVATE DRAFT"},
        ], {"status": "answered", "answer": "PRIVATE ANSWER"}, provider_calls=ctx.provider_traces)
        langsmith_trace._send(config, records)
        assert len(batches) == 1
        assert len(batches[0]["post"]) == (3 if capture_content else 2)
        assert "PRIVATE" not in json.dumps(batches)
        assert batches[0]["post"][1]["name"] == "verify"
        assert ("REQUEST CONTENT" in json.dumps(batches)) is capture_content
        assert ("RESPONSE CONTENT" in json.dumps(batches)) is capture_content
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
