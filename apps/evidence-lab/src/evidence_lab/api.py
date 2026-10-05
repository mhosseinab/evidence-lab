"""Single-operator HTTP API. Only released drafts enter the public answer view."""
from __future__ import annotations

import hmac
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from evidence_lab import __version__
from evidence_lab.config import load_config
from evidence_lab.domain import CallContext, ProviderError
from evidence_lab.ingestion import admit_document, pipeline_revision
from evidence_lab.policy import policy_state
from evidence_lab.retrieval import retrieve_evidence, space_manifest
from evidence_lab.storage import Store
from evidence_lab.graph import graph_descriptor

PUBLIC_RUN_FIELDS = (
    "id", "job_id", "corpus_id", "space_id", "question", "status", "answer", "blocks",
    "checks", "evidence", "qualification", "error", "code", "message", "timings",
    "mode", "config_fingerprint", "created_at", "updated_at", "finished_at", "conversation_id", "graph_steps",
)
JOB_FIELDS = ("id", "kind", "status", "created_at", "updated_at", "finished_at", "attempts", "result", "error")


class UploadBodyLimit:
    """Bound multipart input while receiving it, including chunked uploads."""
    def __init__(self, app, limit):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST" or scope.get("path") != "/api/documents":
            return await self.app(scope, receive, send)
        received = 0

        async def bounded_receive():
            nonlocal received
            event = await receive()
            if event["type"] == "http.request":
                received += len(event.get("body", b""))
                if received > self.limit:
                    raise HTTPException(413, "Upload exceeds the configured limit.")
            return event

        await self.app(scope, bounded_receive, send)


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=1, max_length=16000)
    corpus_id: str = Field(default="default", pattern=r"^[A-Za-z0-9_-]{1,64}$")
    conversation_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class EvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    dataset: str = Field(default="demo", min_length=1, max_length=96)


class CorpusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    corpus_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


def public_run(run: dict) -> dict:
    value = {k: run[k] for k in PUBLIC_RUN_FIELDS if k in run}
    # This guard is independent of the persistence layer's publication guard.
    if value.get("status") != "answered":
        value.update(answer=None, blocks=[])
    return value


def public_job(job: dict) -> dict:
    return {k: job[k] for k in JOB_FIELDS if k in job}


def make_store(config) -> Store:
    return Store(config.database.dsn, limits=config.ingestion.model_dump(mode="json"), memory=config.memory.model_dump())


def create_app(config, *, store=None, hub=None, initialize=True) -> FastAPI:
    """Factory takes resolved config or an explicit YAML path; no hostname inference."""
    from evidence_lab.providers import ProviderHub

    if isinstance(config, (str, Path)):
        config = load_config(config)
    store = store if store is not None else make_store(config)
    owns_hub = hub is None
    hub = hub if hub is not None else ProviderHub(config, store=store)

    @asynccontextmanager
    async def lifespan(app):
        if initialize:
            manifest = space_manifest(config)
            try:
                await run_in_threadpool(store.ensure_corpus, "default", manifest)
            except ProviderError as exc:
                if exc.status != "space_changed":
                    raise
                existing = await run_in_threadpool(store.get_corpus, "default")
                if existing["space_id"] == manifest["id"]:
                    raise
                # An old embedding space remains readable. The operator can
                # create/reindex a separate corpus; retrieval still rejects it
                # under a mismatched embedding configuration before inference.
        yield
        if owns_hub:
            await hub.aclose()

    app = FastAPI(title="Evidence Lab", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(UploadBodyLimit, limit=config.ingestion.max_upload_bytes + 1024**2)
    app.state.config, app.state.store, app.state.hub = config, store, hub

    @app.middleware("http")
    async def operator_boundary(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            token = config.runtime.operator_token
            if token:
                supplied = request.headers.get("authorization", "")
                expected = "Bearer " + token.get_secret_value()
                if not hmac.compare_digest(supplied.encode(), expected.encode()):
                    return JSONResponse({"detail": "An operator token is required.", "code": "unauthorized"},
                                        status_code=401)
            # Prevent another website from driving an unauthenticated localhost app.
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                origin = request.headers.get("origin")
                if request.headers.get("sec-fetch-site") == "cross-site" or (
                    origin and origin.rstrip("/") != str(request.base_url).rstrip("/")
                ):
                    return JSONResponse({"detail": "Cross-origin writes are disabled.", "code": "forbidden"},
                                        status_code=403)
            if request.url.path == "/api/documents" and request.method == "POST":
                length = request.headers.get("content-length")
                if length and (not length.isdigit() or int(length) > config.ingestion.max_upload_bytes + 1024**2):
                    return JSONResponse({"detail": "Upload exceeds the configured limit.", "code": "upload_too_large"},
                                        status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc):
        codes = {"not_found": 404, "invalid_data": 422, "invalid_state": 409,
                 "space_changed": 409, "space_mismatch": 409, "unsupported_type": 415,
                 "unsupported_file": 415, "upload_too_large": 413, "quota_exceeded": 409,
                 "unsupported_document": 415, "invalid_document": 422,
                 "document_too_large": 413, "invalid_encoding": 422,
                 "budget_exhausted": 429, "timeout": 504, "policy_not_ready": 409}
        return JSONResponse({"detail": str(exc), "code": exc.status},
                            status_code=codes.get(exc.status, 503))

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse({"detail": "Request fields are missing or invalid.", "code": "invalid_request"},
                            status_code=422)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        message = "Upload exceeds the configured limit." if exc.status_code == 413 else "The HTTP request could not be completed."
        return JSONResponse({"detail": message, "code": "upload_too_large" if exc.status_code == 413 else "http_error"},
                            status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def internal_error(request, exc):
        # Neither raw provider payloads, DSNs nor Python exception values are public.
        return JSONResponse({"detail": "The operation could not be completed.", "code": "internal_error"},
                            status_code=500)

    @app.get("/health/live")
    def liveness():
        return {"status": "ok", "version": __version__}

    @app.get("/health/ready")
    def readiness():
        ready = store.health()
        return JSONResponse({"status": "ready" if ready else "not_ready", "mode": config.runtime.mode},
                            status_code=200 if ready else 503)

    @app.get("/api/status")
    def status(corpus_id: str = "default"):
        docs = store.list_documents(corpus_id)
        corpus = store.get_corpus(corpus_id)
        state = policy_state(config)
        profiles = {
            role: {"profile": config.role_name(role), "model": config.role_profile(role).model,
                   "protocol": config.role_profile(role).protocol}
            for role in ("embeddings", "generator", "verifier")
        }
        return {"version": __version__, "mode": config.runtime.mode,
                "orchestration": {"engine": "langgraph", "nodes": graph_descriptor()["nodes"],
                                  "edges": [[edge["source"], edge["target"]] for edge in graph_descriptor()["edges"]],
                                  "tools": ["retrieve_corpus_evidence", "preview_evidence_source"],
                                  "memory": {"enabled": config.memory.enabled, "max_turns": config.memory.max_turns},
                                  "tracing": {"provider": "langsmith", "enabled": config.langsmith.enabled, "content": "metadata_only"}},
                "policy_state": state["state"], "policy": state, "profiles": profiles,
                "corpus_id": corpus_id, "corpus": corpus,
                "embedding_space_matches": corpus["space_id"] == space_manifest(config)["id"],
                "counts": {"documents": len(docs), "ready": sum(d.get("state") == "ready" for d in docs),
                           "chunks": sum(d.get("chunk_count", 0) for d in docs)},
                "limits": {"max_file_bytes": config.ingestion.max_upload_bytes,
                           "max_pdf_pages": config.ingestion.max_pdf_pages},
                "budgets": store.budget_summary(), "config_fingerprint": config.fingerprint()}

    @app.get("/api/corpora")
    def corpora():
        return {"corpora": store.list_corpora()}

    @app.post("/api/corpora", status_code=201)
    def create_corpus(payload: CorpusRequest):
        return store.ensure_corpus(payload.corpus_id, space_manifest(config))

    @app.get("/api/documents")
    def documents(corpus_id: str = "default"):
        return {"documents": store.list_documents(corpus_id)}

    @app.post("/api/documents", status_code=202)
    async def upload(file: Annotated[UploadFile, File()], corpus_id: Annotated[str, Form()] = "default",
                     document_id: Annotated[str | None, Form()] = None):
        try:
            raw = await file.read(config.ingestion.max_upload_bytes + 1)
        finally:
            await file.close()
        name = (file.filename or "upload.txt").replace("\\", "/").split("/")[-1]
        media_type = file.content_type or "application/octet-stream"
        admit_document(raw, name, media_type, config)
        await run_in_threadpool(store.ensure_corpus, corpus_id, space_manifest(config))
        return await run_in_threadpool(store.create_document, name, raw, media_type,
                                      corpus_id, document_id, pipeline_revision(config))

    @app.delete("/api/documents/{document_id}")
    def delete_document(document_id: str):
        return store.delete_document(document_id)

    @app.get("/api/source-versions/{version_id}")
    def source_version(version_id: str):
        return store.get_version(version_id)

    @app.get("/api/source-versions/{version_id}/download")
    def source_download(version_id: str):
        version = store.get_version(version_id, include_bytes=True)
        # Attachment plus nosniff keeps active content out of the application origin.
        return Response(content=version["raw"], media_type="application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="source-{version_id}.bin"'})

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        return public_job(store.get_job(job_id))

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        return store.cancel_job(job_id)

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    def retry(job_id: str):
        return public_job(store.retry_job(job_id))

    @app.post("/api/queries", status_code=202)
    def query(payload: QueryRequest):
        if not payload.question.strip():
            raise ProviderError("invalid_data", "A nonempty question is required.")
        state = policy_state(config)
        if config.runtime.mode == "live" and config.verification.mode == "gated" and not state["release_allowed"]:
            raise ProviderError("policy_not_ready", state["reason"])
        settings = {"config_fingerprint": config.fingerprint()}
        if payload.conversation_id is not None:
            settings["conversation_id"] = payload.conversation_id
        return public_run(store.create_run(payload.question, payload.corpus_id, settings))

    @app.post("/api/retrieval/preview")
    async def retrieval_preview(payload: QueryRequest):
        run_id = "preview:" + str(uuid.uuid4())
        ctx = CallContext.for_seconds(run_id, "queries", config.runtime.query_deadline_seconds,
                                      config.runtime.max_remote_attempts_per_query)
        started = time.monotonic()
        evidence = await retrieve_evidence(payload.question, payload.corpus_id, store, hub, config, ctx)
        return {"run_id": run_id, "evidence": evidence.model_dump(mode="json"),
                "seconds": time.monotonic() - started, "mode": config.runtime.mode}

    @app.get("/api/runs")
    def runs(limit: int = 30, corpus_id: str | None = None):
        values = store.list_runs(limit, corpus_id=corpus_id) if corpus_id is not None else store.list_runs(limit)
        return {"runs": [public_run(r) for r in values]}

    @app.get("/api/runs/{run_id}")
    def run(run_id: str):
        return public_run(store.get_run(run_id))

    @app.get("/api/runs/{run_id}/trace")
    def trace(run_id: str):
        return {"label": "Unverified operator diagnostics; drafts are not released answers.",
                "unverified": True, "run": store.get_run(run_id), "calls": store.get_calls(run_id)}

    @app.post("/api/evaluations", status_code=202)
    def start_evaluation(payload: EvaluationRequest):
        if payload.dataset not in config.evaluation.allowlisted_datasets:
            raise ProviderError("invalid_data", "Choose a configured evaluation dataset.")
        value = store.enqueue_job("evaluation", {"dataset": payload.dataset})
        return {"id": value["id"], "job_id": value["id"], "status": value["status"]}

    @app.get("/api/evaluations")
    def evaluations():
        return {"evaluations": [public_job(j) for j in store.list_jobs(kind="evaluation")]}

    @app.get("/api/evaluations/{job_id}")
    def evaluation(job_id: str):
        value = store.get_job(job_id)
        if value["kind"] != "evaluation":
            raise ProviderError("not_found", "Evaluation not found.")
        return public_job(value)

    dashboard = Path.cwd() / "apps/dashboard/dist"
    if (not all((dashboard / name).is_file() for name in ("index.html", "favicon.svg"))
            or not any(dashboard.rglob("*.js")) or not any(dashboard.rglob("*.css"))):
        raise RuntimeError("Dashboard build is missing. Run task dashboard:build from the repository root.")
    app.mount("/static", StaticFiles(directory=dashboard), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(dashboard / "index.html")

    return app
