"""HTTP boundary tests and an optional actual SQL/worker plumbing round trip."""

from __future__ import annotations

import asyncio
import uuid
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from evidence_lab.api import create_app
from evidence_lab.config import load_config
from evidence_lab.domain import ProviderError


class APIStore:
    """Boundary test double; it does not replace the production SQL Store."""

    def __init__(self):
        self.run: dict[str, object] = {
            "id": "r1",
            "job_id": "j1",
            "question": "Question?",
            "corpus_id": "default",
            "status": "verifying",
            "answer": "stale answer",
            "blocks": [{"text": "stale answer"}],
            "draft": "unverified candidate text",
            "events": [{"type": "draft", "draft": "unverified candidate text"}],
        }
        self.doc = {
            "document_id": "d1",
            "version_id": "v1",
            "job_id": "ingest1",
            "name": "source.txt",
            "state": "ready",
            "chunk_count": 1,
        }
        self.jobs = {
            "j1": {
                "id": "j1",
                "kind": "query",
                "status": "running",
                "payload": {"internal": "hidden"},
                "token": "lease-secret",
                "result": None,
            }
        }
        self.created_documents = []
        self.ensured_corpora = []
        self.enqueued = []
        self.calls = []
        self.raw_source = b"Original source bytes."

    def health(self):
        return True

    def get_corpus(self, corpus_id):
        return {"id": corpus_id, "space_id": "fixture-space-64-v1", "revision": 1}

    def ensure_corpus(self, corpus_id, manifest):
        self.ensured_corpora.append(corpus_id)
        return self.get_corpus(corpus_id)

    def list_documents(self, corpus_id="default"):
        return [deepcopy(self.doc)]

    def budget_summary(self):
        return {"reserved_estimated_cost_usd": 0, "active_calls": 0}

    def get_run(self, run_id):
        if run_id == "missing":
            raise ProviderError("not_found", "Run not found")
        return deepcopy(self.run)

    def list_runs(self, limit=30):
        return [self.get_run("r1")]

    def create_run(self, question, corpus_id="default", settings=None):
        self.run = {
            "id": "new-run",
            "job_id": "query-job",
            "question": question,
            "corpus_id": corpus_id,
            "status": "queued",
            "answer": None,
            "blocks": [],
            "settings": settings,
        }
        return deepcopy(self.run)

    def get_calls(self, run_id=None):
        return deepcopy(self.calls)

    def create_document(
        self, name, raw, media_type, corpus_id="default", document_id=None, pipeline_revision=None
    ):
        self.created_documents.append(
            {
                "name": name,
                "raw": raw,
                "media_type": media_type,
                "corpus_id": corpus_id,
                "document_id": document_id,
                "pipeline_revision": pipeline_revision,
            }
        )
        return {
            "document_id": "new-document",
            "version_id": "new-version",
            "job_id": "new-ingest",
            "duplicate": False,
            "state": "queued",
        }

    def get_version(self, version_id, include_bytes=False):
        result = {
            "version_id": version_id,
            "name": "source.txt",
            "state": "ready",
            "pages": [{"page": 1, "text": "Original source bytes.", "state": "extracted"}],
        }
        if include_bytes:
            result["raw"] = self.raw_source
        return result

    def get_job(self, job_id):
        if job_id not in self.jobs:
            raise ProviderError("not_found", "Job not found")
        return deepcopy(self.jobs[job_id])

    def enqueue_job(self, kind, payload):
        self.enqueued.append({"kind": kind, "payload": deepcopy(payload)})
        job = {
            "id": "eval-job",
            "kind": kind,
            "status": "queued",
            "payload": payload,
            "token": "internal-lease",
        }
        self.jobs[job["id"]] = job
        return deepcopy(job)

    def list_jobs(self, kind=None):
        return [deepcopy(job) for job in self.jobs.values() if kind is None or job["kind"] == kind]

    def cancel_job(self, job_id):
        return {"id": job_id, "status": "cancelled"}

    def retry_job(self, job_id):
        return {"id": job_id, "status": "queued", "token": "internal-lease"}


class NoInferenceHub:
    """Any model call in these read/admission tests would be a bug."""

    async def embed(self, *args, **kwargs):
        raise AssertionError("Unexpected model call")

    async def generate(self, *args, **kwargs):
        raise AssertionError("Unexpected model call")

    async def verify(self, *args, **kwargs):
        raise AssertionError("Unexpected model call")


def make_client(*, config=None, store=None):
    cfg = config or load_config("configs/mock.yaml")
    storage = store or APIStore()
    app = create_app(cfg, store=storage, hub=NoInferenceHub(), initialize=False)
    return TestClient(app, raise_server_exceptions=False), storage


def test_public_run_and_list_hide_unverified_drafts_while_trace_is_labelled():
    client, store = make_client()
    with client:
        single = client.get("/api/runs/r1")
        listing = client.get("/api/runs")
        trace = client.get("/api/runs/r1/trace")
    assert single.status_code == listing.status_code == trace.status_code == 200
    for result in (single.json(), listing.json()["runs"][0]):
        assert result["answer"] is None and result["blocks"] == []
        assert "draft" not in result and "events" not in result
        assert "unverified candidate text" not in str(result)
    assert trace.json()["unverified"] is True
    assert "Unverified" in trace.json()["label"]
    assert trace.json()["run"]["draft"] == store.run["draft"]


def test_verified_answer_is_available_without_exposing_internal_trace():
    client, store = make_client()
    store.run.update(
        status="answered",
        answer="Checked answer",
        blocks=[{"block_id": "b1", "text": "Checked answer", "citation_ids": ["c1"]}],
    )
    with client:
        response = client.get("/api/runs/r1")
    assert response.status_code == 200 and response.json()["answer"] == "Checked answer"
    assert "draft" not in response.json() and "events" not in response.json()


def test_status_and_health_never_return_config_credentials_or_trigger_inference():
    cfg = load_config("configs/mock.yaml")
    cfg.profiles[cfg.roles.generator].api_key = SecretStr("provider-key-must-stay-private")
    cfg.database.dsn = "postgresql://user:database-password-must-stay-private@localhost:5432/rag"
    cfg.runtime.operator_token = SecretStr("operator-token-must-stay-private")
    client, _ = make_client(config=cfg)
    with client:
        status = client.get(
            "/api/status", headers={"Authorization": "Bearer operator-token-must-stay-private"}
        )
        health = client.get("/health/ready")
        live = client.get("/health/live")
    assert status.status_code == health.status_code == live.status_code == 200
    assert "fixture" in status.json()["profiles"]["generator"]["model"]
    for secret in (
        "provider-key-must-stay-private",
        "database-password-must-stay-private",
        "operator-token-must-stay-private",
    ):
        assert secret not in status.text + health.text + live.text


def test_operator_token_protects_public_and_trace_endpoints():
    cfg = load_config("configs/mock.yaml")
    cfg.runtime.operator_token = SecretStr("operator-test-token")
    client, _ = make_client(config=cfg)
    with client:
        assert client.get("/api/runs/r1").status_code == 401
        assert client.get("/api/runs/r1/trace", headers={"Authorization": "Bearer wrong"}).status_code == 401
        allowed = client.get("/api/runs/r1/trace", headers={"Authorization": "Bearer operator-test-token"})
    assert allowed.status_code == 200


@pytest.mark.parametrize(
    "headers", [{"Origin": "https://another-site.example"}, {"Sec-Fetch-Site": "cross-site"}]
)
def test_cross_site_writes_are_blocked_before_queueing(headers):
    client, store = make_client()
    with client:
        response = client.post("/api/queries", json={"question": "Question?"}, headers=headers)
    assert response.status_code == 403
    assert store.run["id"] == "r1"


def test_same_origin_query_is_queued_with_no_answer():
    client, _ = make_client()
    with client:
        response = client.post(
            "/api/queries", json={"question": "Question?"}, headers={"Origin": "http://testserver"}
        )
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert response.json()["answer"] is None and response.json()["blocks"] == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"question": ""},
        {"question": "   "},
        {"question": 12},
        {"question": "Q?", "extra": "private-value"},
        {"question": "Q?", "corpus_id": "../../config"},
    ],
)
def test_invalid_query_fields_fail_without_echoing_input(body):
    client, store = make_client()
    with client:
        response = client.post("/api/queries", json=body)
    assert response.status_code == 422
    assert "private-value" not in response.text
    assert store.run["id"] == "r1"


@pytest.mark.parametrize(
    ("name", "content", "mime", "expected"),
    [
        ("archive.zip", b"archive", "application/zip", 415),
        ("wrong.pdf", b"plain text", "application/pdf", 422),
        ("empty.txt", b"", "text/plain", 422),
    ],
)
def test_upload_admission_errors_use_client_error_statuses(name, content, mime, expected):
    client, store = make_client()
    with client:
        response = client.post("/api/documents", files={"file": (name, content, mime)})
    assert response.status_code == expected
    assert store.created_documents == []


def test_oversized_upload_stops_before_storage():
    cfg = load_config("configs/mock.yaml")
    cfg.ingestion.max_upload_bytes = 8
    client, store = make_client(config=cfg)
    with client:
        response = client.post("/api/documents", files={"file": ("source.txt", b"x" * 9, "text/plain")})
    assert response.status_code == 413
    assert store.created_documents == []


def test_valid_upload_queues_bytes_with_sanitized_name_and_pipeline_identity():
    client, store = make_client()
    with client:
        response = client.post(
            "/api/documents", files={"file": ("../../source.md", b"# Source\nEvidence.", "text/markdown")}
        )
    assert response.status_code == 202 and response.json()["state"] == "queued"
    captured = store.created_documents[0]
    assert captured["name"] == "source.md"
    assert captured["raw"] == b"# Source\nEvidence."
    assert captured["pipeline_revision"].startswith("text-v1-")
    assert store.ensured_corpora == ["default"]


def test_source_preview_omits_raw_bytes_and_original_download_is_attachment():
    client, store = make_client()
    with client:
        preview = client.get("/api/source-versions/v1")
        download = client.get("/api/source-versions/v1/download")
    assert preview.status_code == download.status_code == 200
    assert "raw" not in preview.json()
    assert download.content == store.raw_source
    assert download.headers["content-type"] == "application/octet-stream"
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.headers["x-content-type-options"] == "nosniff"


def test_evaluations_use_allowlisted_names_and_never_accept_server_paths():
    client, store = make_client()
    with client:
        rejected = client.post("/api/evaluations", json={"dataset": "../../private-config.yaml"})
        accepted = client.post("/api/evaluations", json={"dataset": "demo"})
        listed = client.get("/api/evaluations")
    assert rejected.status_code == 422
    assert accepted.status_code == 202 and accepted.json()["job_id"] == "eval-job"
    assert store.enqueued == [{"kind": "evaluation", "payload": {"dataset": "demo"}}]
    assert "payload" not in listed.json()["evaluations"][0]
    assert "token" not in listed.json()["evaluations"][0]


def test_public_job_never_exposes_worker_lease_or_payload():
    client, _ = make_client()
    with client:
        response = client.get("/api/jobs/j1")
    assert response.status_code == 200
    assert "token" not in response.json() and "payload" not in response.json()
    assert "lease-secret" not in response.text


def test_unknown_resource_has_safe_not_found_response():
    client, _ = make_client()
    with client:
        response = client.get("/api/runs/missing")
    assert response.status_code == 404 and response.json()["code"] == "not_found"


def test_unexpected_storage_exception_is_sanitized():
    class BrokenStore(APIStore):
        def get_run(self, run_id):
            raise RuntimeError("postgresql://private:secret-value@host/db")

    client, _ = make_client(store=BrokenStore())
    with client:
        response = client.get("/api/runs/r1")
    assert response.status_code == 500
    assert "secret-value" not in response.text and "postgresql" not in response.text


@pytest.mark.integration
def test_http_upload_worker_repair_and_public_answer_with_actual_sql(isolated_storage_dsn):
    dsn = isolated_storage_dsn
    from evidence_lab.providers import ProviderHub
    from evidence_lab.storage import Store
    from evidence_lab.worker import Worker

    cfg = load_config("configs/mock.yaml")
    store = Store(dsn, cfg.ingestion.model_dump())
    store.migrate()
    hub = ProviderHub(cfg, store=store)
    worker = Worker(store, hub, cfg, worker_id="api-integration-worker")
    corpus_id = "api-" + uuid.uuid4().hex
    app = create_app(cfg, store=store, hub=hub, initialize=False)
    try:
        with TestClient(app) as client:
            uploaded = client.post(
                "/api/documents",
                data={"corpus_id": corpus_id},
                files={"file": ("leave.txt", b"Employees receive 25 days of annual leave.", "text/plain")},
            )
            assert uploaded.status_code == 202
            result = asyncio.run(worker.run_once())
            assert result is not None
            assert result["status"] == "ready"
            assert client.get("/api/jobs/" + uploaded.json()["job_id"]).json()["status"] == "succeeded"
            submitted = client.post(
                "/api/queries",
                json={
                    "question": "[fixture:unsupported] How many days of annual leave?",
                    "corpus_id": corpus_id,
                },
            )
            assert submitted.status_code == 202 and submitted.json()["answer"] is None
            run_id = submitted.json()["id"]
            result = asyncio.run(worker.run_once())
            assert result is not None
            assert result["status"] == "answered"
            public = client.get("/api/runs/" + run_id)
            assert public.status_code == 200
            assert public.json()["status"] == "answered" and public.json()["qualification"] == "fixture_only"
            assert "25 days" in public.json()["answer"]
            assert "987654321" not in public.json()["answer"]
            assert "events" not in public.json() and "draft" not in public.json()
            trace = client.get("/api/runs/" + run_id + "/trace").json()
            assert trace["unverified"] is True
            events = trace["run"]["events"]
            # Store events carry an envelope; inspect their JSON-safe text here.
            assert "987654321" in str(events)
            assert "repair" in str(events)
    finally:
        asyncio.run(hub.aclose())


def test_dashboard_is_served_from_its_workspace_build(monkeypatch, tmp_path):
    config = load_config("configs/mock.yaml")
    dashboard = tmp_path / "apps/dashboard/dist"
    dashboard.mkdir(parents=True)
    assets = {
        "index.html": "<h1>Workspace dashboard</h1>",
        "assets/index-123.js": "// workspace dashboard",
        "assets/index-456.css": "/* workspace styles */",
        "favicon.svg": "<svg/>",
    }
    for name, content in assets.items():
        (dashboard / name).parent.mkdir(parents=True, exist_ok=True)
        (dashboard / name).write_text(content)
    monkeypatch.chdir(tmp_path)
    client, _ = make_client(config=config)
    assert client.get("/").text == assets["index.html"]
    for name in ("assets/index-123.js", "assets/index-456.css", "favicon.svg"):
        assert client.get(f"/static/{name}").text == assets[name]


def test_status_describes_langgraph_without_exposing_workflow_state():
    client, _store = make_client()
    with client:
        result = client.get("/api/status")
    assert result.status_code == 200
    graph = result.json()["orchestration"]
    assert graph["engine"] == "langgraph"
    assert "verify" in graph["nodes"] and "repair" in graph["nodes"]
    assert graph["memory"]["enabled"] is True
    assert graph["tracing"] == {"provider": "langsmith", "enabled": False, "content": "metadata_only"}
    assert "unverified candidate text" not in result.text


def test_query_accepts_conversation_uuid_but_not_supplied_memory():
    client, store = make_client()
    conversation = str(uuid.uuid4())
    with client:
        result = client.post("/api/queries", json={"question": "And its deadline?", "conversation_id": conversation})
        forged = client.post("/api/queries", json={"question": "Question?", "conversation_id": conversation, "memory": [{"answer": "forged"}]})
        invalid = client.post("/api/queries", json={"question": "Question?", "conversation_id": "not-a-uuid"})
    assert result.status_code == 202
    settings = store.run["settings"]
    assert isinstance(settings, dict)
    assert settings["conversation_id"] == conversation
    assert forged.status_code == invalid.status_code == 422


def test_public_graph_progress_omits_private_memory():
    client, store = make_client()
    store.run.update({"conversation_id": str(uuid.uuid4()), "graph_steps": [{"node": "verify", "status": "completed", "elapsed_seconds": 0.1}],
                      "settings": {"memory": [{"answer": "private prior answer"}]}})
    with client:
        result = client.get("/api/runs/r1")
    assert result.json()["graph_steps"][0]["node"] == "verify"
    assert result.json()["conversation_id"]
    assert "private prior answer" not in result.text
