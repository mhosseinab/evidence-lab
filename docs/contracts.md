# Shared implementation interfaces

Domain models in `apps/evidence-lab/src/evidence_lab/domain.py` (imported as `evidence_lab.domain`) are authoritative. The application is the `apps/evidence-lab` member of the root uv workspace; run commands from the repository root after `task setup`. Use JSON-safe dictionaries at persistence boundaries. All IDs are strings. Times returned by Store must be JSON serializable ISO timestamps or epoch values.

## Configuration and providers

`evidence_lab.config.load_config(path) -> AppConfig`; Pydantic object with runtime, database, ingestion, retrieval, verification, budgets, profiles, roles, evaluation settings from plan. `config.safe_dict()` returns redacted config; `config.fingerprint()` returns sanitized hash. Database DSN is configurable, default `postgresql://evidence:evidence@localhost:5432/evidence_lab` (local sample only). Mock config must be complete and runnable; provider profiles still describe real contracts, runtime.mode=mock selects deterministic fixtures without HTTP/model downloads.

`evidence_lab.providers.ProviderHub(config, store=None, client=None)` provides async methods:

* `embed(texts: list[str], ctx: CallContext) -> list[list[float]]`
* `generate(question: str, evidence: EvidencePack, ctx: CallContext, repair: dict | None = None) -> Draft`
* `generate_candidate(question: str, evidence: EvidencePack, ctx: CallContext) -> dict` for the paired evaluator only: text, decoded object and candidate_hash. Universal decoding/runtime limits are shared; schema rejection begins in B and in interactive typed generation.
* `verify(question: str, draft: Draft, evidence: EvidencePack, ctx: CallContext, round_id: str = "initial") -> VerificationResult`
* `aclose()`

Use ProviderError with a safe status/message. Missing/foreign/duplicate check IDs must fail. Bind hashes and round ID in application code, not by trusting model echoes. Mock generation/verification must be explicitly marked fixture-only, use exact evidence excerpts and deterministic adversarial cases. Do not claim mocked validation proves semantic quality. Native Clef uses actual documented choice/noul schema after checking official docs. All optional protocol settings come from config. No auto fallback. Budget/call hooks below.

## Store

`evidence_lab.storage.Store(dsn: str, limits: dict | None = None)` uses sync psycopg connections, short transactions. Public methods:

* `migrate()` and `health() -> bool`
* `ensure_corpus(corpus_id: str, space: dict) -> dict` where space has id, dimensions, model, fingerprint. Never silently switch an existing active space.
* `get_corpus(corpus_id) -> dict` with id, space_id, revision.
* `create_document(name, raw: bytes, media_type, corpus_id="default", document_id=None, pipeline_revision=...) -> dict` with document_id, version_id, job_id, duplicate. Enqueue ingestion atomically. Payload contains version_id, corpus_id.
* `list_documents(corpus_id="default") -> list[dict]`
* `get_version(version_id, include_bytes=False) -> dict`
* `save_extraction(version_id, pages: list[dict], chunks: list[dict], state: str, lease: dict)`. Chunks keys id, text, page, start, end, text_hash.
* `chunks_for_version(version_id) -> list[dict]`
* `get_cached_embeddings(space_id, hashes: list[str]) -> dict[str,list[float]]`
* `write_embeddings(version_id, space_id, vectors: dict[str,list[float]], lease: dict)` keyed by chunk ID.
* `activate_version(version_id, space_id, lease: dict)` guards current job lease, intended source version and complete valid vectors.
* `retrieve(corpus_id, space_id, query, vector, dense_limit=40, lexical_limit=40) -> dict` with corpus_revision, dense, lexical. Each candidate has EvidenceItem fields plus rank and score. One repeatable-read snapshot and same active source/space filters. Raises if space changed.
* `create_run(question, corpus_id="default", settings=None) -> dict` creates query run and query job, returns id and job_id.
* `update_run(run_id, fields: dict, lease: dict | None = None)` lease required to publish any worker-owned result. Root API never publishes answers.
* `get_run(run_id) -> dict`, `list_runs(limit=30, corpus_id=None) -> list[dict]`, `append_event(run_id,event:dict,lease=None)`, `list_corpora() -> list[dict]`.
* `enqueue_job(kind,payload) -> dict`, `claim_job(worker_id,lease_seconds=120) -> dict|None`, `renew_job(job_id,token,lease_seconds=120) -> bool`, `finish_job(job_id,token,status,result=None,error=None)`, `get_job(job_id)`, `cancel_job(job_id)`. Claimed job contains id, token, kind, payload, created_at, attempts. All state writes use lease fencing; stale workers cannot publish.
* `reserve_call(run_id,phase,profile,estimated_cost,limits:dict) -> str` call ID. Limits keys total_cap, phase_caps, run_attempt_cap, remote_concurrency, optional profile_concurrency (defaults to the global cap), mock(bool), timeout_seconds. Serialize reservations with advisory lock; count unknown costs conservatively, enforce attempts and global/per-profile active concurrency across workers. Throw safe errors; no waiting while holding transaction.
* `finish_call(call_id,status,usage=None,actual_cost=None,detail=None)` preserve reservation if usage/cost unknown. Store metadata only, no keys/auth headers.
* `get_calls(run_id=None) -> list[dict]`
* Optional deletion/cleanup and evaluation job result support, documented if added.

Budget ledger must not reset at worker restart. Expired active reservations release concurrency, retain conservative cost.

## Ingestion and retrieval

`evidence_lab.ingestion.extract_document(raw,name,media_type,config) -> dict` with pages, chunks, state and errors. `async ingest_job(job,store,hub,config) -> dict` handles staged resumable batches; passes job as lease; returns status/IDs. Strictly no OCR/model downloads. Preserve text and stable coordinates. Pipeline identity part of idempotency.

`evidence_lab.retrieval.fuse_candidates(dense,lexical,k=60) -> list[EvidenceItem]`; deterministic ties and overlap deduplication.
`async retrieve_evidence(question,corpus_id,store,hub,config,ctx) -> EvidencePack`; pin space before query embedding, snapshot recheck, pack to shared context budget without silently truncating after generation. `space_manifest(config) -> dict` supplies id/dimensions/model/fingerprint for corpus creation. Coordinate exact config attributes with provider owner.

## Gate and orchestration owned by root

`evidence_lab.policy.structural_check(draft,evidence)`; `evaluate_checks(draft,evidence,result,threshold=None) -> dict` with accepted, failures; policy manifest binds semantic profile/settings. `evidence_lab.engine.QueryEngine(store,hub,config).run(job)` executes question, stores trace, at most one repair, terminal result. Public API strips unverified drafts/raw traces.

## Evaluation

Module `evidence_lab.evaluation` owns schemas, paired A/B/C/D runner, controlled verifier challenges, metrics with explicit denominators, intervals and honest qualification. Store evaluation runs as durable jobs (`kind=evaluation`) with result in job result; root worker calls `async run_evaluation(job,store,hub,config) -> dict`. Provide helper pure metric APIs and CLI-compatible `async evaluate_dataset(path,store,hub,config,...)` if useful; coordinate with root. Dataset CLI path supplied by operator only, API must choose an allowlisted bundled dataset, never arbitrary server paths. Bundle small explicitly synthetic fixture corpus/question set, human review templates and mutation generator. Do not fabricate 300 human-reviewed cases or pass a mock study as model qualification.

## HTTP/UI

API routes from plan plus GET /api/status, GET /api/documents, GET /api/runs, GET /api/evaluations, POST /api/jobs/{id}/cancel, GET /api/runs/{id}/trace. Document upload uses multipart `file`, `corpus_id=default`, optional document_id. Query POST JSON `{question,corpus_id:"default"}`. Source preview `/api/source-versions/{id}`; download original at `/api/source-versions/{id}/download`. Evaluation POST JSON `{dataset:"demo"}`. Use JSON errors `{detail: safe string, code:...}`. Upload/query/eval mutations return202 with IDs; polling terminal statuses. GET /api/status exposes mode, policy state, corpus counts, redacted profile names/model IDs, budget summaries, no secrets. Auth optional operator token in config; default bind localhost. UI uses safe textContent, no unsanitized innerHTML for uploaded/model text.

The Vue/TypeScript dashboard consumes these HTTP contracts through runtime-validated helpers in `apps/dashboard/src/api/` and types in `src/types/`. Components use Vue interpolation for untrusted text; answer views preserve the release gate and never render private drafts. Views live in `src/views/`, shared components in `src/components/`, state/actions in `src/composables/`, and CSS in `src/assets/`; `task dashboard:build` creates `apps/dashboard/dist/`, which FastAPI serves directly at `/` and `/static` from the repository root. `task dashboard:serve` starts this combined API/dashboard service. Run `task dashboard:typecheck` and `task dashboard:test` when changing dashboard request/response handling. Generated static files are build outputs, not contract sources.

## LangGraph, conversation memory and telemetry

QueryEngine.run(job) is the single compiled LangGraph path. integrations provides LedgerChatModel (BaseChatModel), LedgerEmbeddings, native verifier Runnable and corpus-scoped StructuredTools. ProviderHub/CallExecutor continue to own every outbound call, retry and spend reservation. No model-driven arbitrary tools are executed.

POST /api/queries accepts optional conversation_id (UUID); the server creates a corpus-bound conversation when absent. Store(dsn, limits=None, memory=None) snapshots only completed answered pairs at acceptance, overwriting supplied memory. Source deletion scrubs copied context and fences pending runs. The public run adds conversation_id and graph_steps [{node,status,elapsed_seconds}]; memory snapshots and graph state remain private.

GET /api/status adds orchestration {engine,nodes,edges,tools,memory,tracing}; the dashboard consumes this descriptor and persisted execution steps. LangSmith export is explicitly enabled through YAML with one direct/environment credential source, metadata only, and fails open independently of answer publication. Ambient tracing is disabled. Memory is durable SQL state; LangGraph checkpointers are not used and job recovery may restart a nonterminal query.

The fresh baseline contains evidence_* tables, including corpus-bound conversations. Environment variables use EVIDENCE_LAB_*; previous schema and environment aliases are unsupported.
