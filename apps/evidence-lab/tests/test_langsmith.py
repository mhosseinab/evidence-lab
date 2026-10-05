"""Only explicit, allowlisted workflow metadata can leave through LangSmith."""
import asyncio
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from evidence_lab.config import ConfigError, load_config
from evidence_lab import langsmith_trace


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
    assert config.langsmith.api_key.get_secret_value() == "private-trace-key"
    assert "private-trace-key" not in json.dumps(config.safe_dict())


def test_disabled_langsmith_redacts_url_credentials(tmp_path):
    data = yaml.safe_load(Path("configs/mock.yaml").read_text())
    data["langsmith"] = {"enabled": False, "api_url": "https://operator:PRIVATE@traces.test/?key=PRIVATE"}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    assert "PRIVATE" not in json.dumps(load_config(path).safe_dict())


def test_real_sdk_exports_only_allowlisted_metadata_to_selected_endpoint():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from pydantic import SecretStr

    batches = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
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
        records = langsmith_trace.trace_records(config, "run1", [
            {"node": "verify", "status": "completed", "elapsed_seconds": 0.1,
             "question": "PRIVATE QUESTION", "draft": "PRIVATE DRAFT"},
        ], {"status": "answered", "answer": "PRIVATE ANSWER"})
        langsmith_trace._send(config, records)
        assert len(batches) == 1
        assert len(batches[0]["post"]) == 2
        assert "PRIVATE" not in json.dumps(batches)
        assert batches[0]["post"][1]["name"] == "verify"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
