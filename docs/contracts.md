# Shared implementation interfaces

Domain models in `apps/evidence-lab/src/evidence_lab/domain.py` (imported as `evidence_lab.domain`) are authoritative. The application is the `apps/evidence-lab` member of the root uv workspace; run commands from the repository root after `task setup`. Use JSON-safe dictionaries at persistence boundaries. All IDs are strings. Times returned by Store must be JSON serializable ISO timestamps or epoch values.

Completed ingestion jobs return `result.chunks` as a nonnegative integer count;
source previews return `chunks` as a list of chunk objects. The dashboard validates
both forms and refreshes documents after observing a terminal ingestion job.

## Configuration and providers

`evidence_lab.config.load_config(path) -> AppConfig`; Pydantic object with runtime, database, ingestion, retrieval, verification, budgets, profiles, roles, evaluation settings from plan. `config.safe_dict()` returns redacted config; `config.fingerprint()` returns sanitized hash. Database DSN is configurable, default `postgresql://evidence:evidence@localhost:5432/evidence_lab` (local sample only). Mock config must be complete and runnable; provider profiles still describe real contracts, runtime.mode=mock selects deterministic fixtures without HTTP/model downloads.

`evidence_lab.providers.ProviderHub(config, store=None, client=None)` provides async methods:

* `embed(texts: list[str], ctx: CallContext) -> list[list[float]]`
* `generate(question: str, evidence: EvidencePack, ctx: CallContext, repair: dict | None = None) -> Draft`
* `generate_candidate(question: str, evidence: EvidencePack, ctx: CallContext) -> dict` for the paired evaluator only: text, decoded object and candidate_hash. Universal decoding/runtime limits are shared; schema rejection begins in B and in interactive typed generation.
* `verify(question: str, draft: Draft, evidence: EvidencePack, ctx: CallContext, round_id: str = "initial") -> VerificationResult`
* `aclose()`

Use ProviderError with a safe status/message. Missing/foreign/duplicate check IDs must fail. Bind hashes and round ID in application code, not by trusting model echoes. Mock generation/verification must be explicitly marked fixture-only, use exact evidence excerpts and deterministic adversarial cases. Do not claim mocked validation proves semantic quality. Native Clef uses its documented choice/probability schema. All optional protocol settings come from config. No auto fallback. Budget/call hooks below.

## Store

`evidence_lab.storage.Store(dsn: str, limits: dict | None = None)` uses sync psycopg connections, short transactions. Public methods:

* `migrate()` and `health() -> bool`
* `ensure_corpus(corpus_id: str, space: dict) -> dict` where space has id, dimensions, model, fingerprint. Never silently switch an existing active space.
* `get_corpus(corpus_id) -> dict` with id, space_id, revision.
* `delete_corpus(corpus_id) -> dict` with corpus_id, deleted=true, purged_documents, purged_runs and purged_jobs. One transaction purges source bytes/versions/chunks/vectors, corpus runs/events/conversations, ingestion/query jobs and related evaluation jobs/results, unused cached embeddings and an unreferenced embedding space. Other corpora and their shared cache entries survive. Missing corpus raises not_found. Active evaluations without corpus provenance block deletion with invalid_state until cancelled or finished. Worker publication and further call reservations are fenced after purge. Global spend and active-call accounting survive anonymously without run/corpus linkage, usage or detail, so deletion cannot reset the configured budget; late completions cannot restore purged context. External LangSmith traces and backups are outside this database purge. The default corpus can be deleted but application startup may recreate an empty default.
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

QueryEngine.run(job) is the single compiled LangGraph path. integrations provides LedgerChatModel (BaseChatModel), LedgerEmbeddings, scoped verification StructuredTool and corpus-scoped StructuredTools. ProviderHub/CallExecutor continue to own every outbound call, retry and spend reservation. No model-driven arbitrary tools are executed.

POST /api/queries accepts optional conversation_id (UUID); the server creates a corpus-bound conversation when absent. Store(dsn, limits=None, memory=None) snapshots only completed answered pairs at acceptance, overwriting supplied memory. Source deletion scrubs copied context and fences pending runs. The public run adds conversation_id and graph_steps [{node,status,elapsed_seconds}]; memory snapshots and graph state remain private.

GET /api/status adds orchestration {engine,nodes,edges,tools,memory,tracing}; the dashboard consumes this descriptor and persisted execution steps. LangSmith export is explicitly enabled through YAML with one direct/environment credential source and fails open independently of answer publication. `langsmith.capture_content` defaults to false; explicit opt-in additionally exports redacted provider request/response bodies held in `CallContext.provider_traces`, including rejected responses. These ephemeral records never enter the persistent call ledger. `/api/status` reports tracing content as `metadata_only` or `provider_payloads`. Ambient tracing is disabled. Memory is durable SQL state; LangGraph checkpointers are not used and job recovery may restart a nonterminal query.

The fresh baseline contains evidence_* tables, including corpus-bound conversations. Environment variables use EVIDENCE_LAB_*; previous schema and environment aliases are unsupported.


### Verification tool

`integrations.tools.verification_tool(hub, evidence, context, round_id="initial")`
returns the asynchronous `verify_frozen_evidence` StructuredTool. Strict arguments
are `{question, draft}`; evidence is deep-copied at construction, and the profile,
ledger context and round are bound by the application. Native Clef and chat/mock
verifiers reuse `ProviderHub.verify`; no new transport or retry owner exists.
A standard LangChain ToolCall returns a ToolMessage containing JSON checks/hashes/
round/status without raw provider data, plus a full typed VerificationResult in
`artifact`. Plain argument invocation returns only the summary dictionary.
The graph consumes the artifact and applies `evaluate_checks`; tool output alone
never authorizes release. Provider/validation failures propagate rather than
becoming successful verdicts. Ambient tracing stays disabled; explicit callbacks
remain supported. Clef choice probabilities retain their uncalibrated semantics.

Qualification identity includes effective embedding execution mode. Switching
between live and deterministic query embeddings invalidates a previously matched
policy; equivalent implicit and explicit live embedding mode remain identical.

## Browser credentials (BYOK)

The application currently has a shared workspace and a shared operator credential,
not per-user identity or document permissions. All users with workspace access
share its files. Dashboard notices communicate this; [workspace isolation](todo.md)
is future work. See [BYOK user setup](byok.md) for the dashboard workflow.

`runtime.credentials` is `server` by default. In YAML live `browser` mode active
provider profiles must omit all credential fields; startup never reads provider
secrets. `GET /api/status` includes `credentials`, `byok_key_scope` (the base
configuration fingerprint), `spending_limit_usd` and `byok_profiles`, a list of
`{name, model, roles, key_group}` for every active profile. Key groups are `llm`
and `cloudflare`; native Clef uses the latter.

The dashboard sends `X-Evidence-Lab-Mode: mock|live` with requests. Unsigned browser live requests
also carry bounded JSON `X-Evidence-Lab-Live-Settings`: complete embedding/chat
endpoints and model IDs, embedding dimensions, embedding/chat input limits, chat
output limit, JSON output format, output token parameter, Cloudflare account ID,
nonnegative budget and embedding input/chat input/chat output prices per million
tokens. `embedding_source` is `custom` by default (including older saved setup),
or `workspace` to preserve the base embedding profile and vector manifest.
Workspace setup omits custom embedding fields. Query embeddings use the base
profile's effective execution mode; generation and Clef remain live. The status
reports `embedding_mode` and `embedding_source`. Mock embedding profiles are
excluded from required browser credentials and stay visibly labeled fixtures. Strict validation rejects incomplete setup with safe errors. A request
selects an isolated runtime without mutating the server configuration. Selected
live mode uses browser credentials, Clef verification and an unqualified shadow
policy. Mode validation only reads status metadata and makes no model calls.
Operator sessions omit mode/setup/key headers and use the server configuration.
Server-funded API inference requires a configured operator token and an authenticated
request. Missing or incorrect tokens return 401 before inference or job mutation.
Configured operator tokens must contain non-whitespace characters. Empty or blank
tokens are rejected during configuration validation; the inference guard also
rejects them when an explicit configuration object bypasses validation.
Omitting the token from a live server-credential configuration also returns 401
for queries, uploads, evaluations, retrieval previews and retries. Mock fixtures
and browser-owned credentials do not acquire server keys. CLI and workers remain
trusted operator processes, independent of dashboard sign-in.
Operator tokens stay in page memory, validated before adopting server mode;
failed sign-in restores previous browser state. Sign-out removes the token and
restores saved browser preferences if access is available.

Inference POSTs (document upload, query, retrieval preview, evaluation and job
retry) require `X-Evidence-Lab-Provider-Keys`, a bounded JSON object containing
`llm` and, when required, `cloudflare` printable non-placeholder keys. Legacy
per-profile payloads remain accepted. Validation fails before enqueuing work.
API tasks use isolated config copies and existing worker dispatch, leases,
adapters and budget ledger. Credentials do not change configuration or policy
fingerprints and never enter durable payloads. The dashboard saves keys, setup
and mode locally per base configuration fingerprint. Keys only accompany
inference endpoints, never status/trace/source requests.

Per-insertion browser credential options (or `Store(..., browser_credentials=True)`) atomically mark newly inserted job payloads with
`browser_credentials: true`. General `claim_job` skips them;
`claim_job(..., browser_job_id=id)` leases only that named browser job, including
expired leases. API-created background tasks discard their scoped keys after
completion. Recovery requires a browser request to resupply credentials.

Retaining an external workspace embedding profile in browser BYOK mode strips
its server credential references and authenticates it with the browser LLM key.
Separate server and browser provider credentials are not combined. Signed-in
operators use the server configuration normally.

## Agent RAG over MCP

`/api/mcp/` serves stateless Streamable HTTP using MCP SDK 2.3.0. All requests
require the configured operator bearer token, including in fixture mode. Origin
checks use the SDK's configured allowlist after authentication; REST writes retain
same-origin protection. An explicit Host allowlist applies. This is a private trusted-operator
interface, not scoped OAuth delegation; token holders retain ordinary operator
REST privileges. `agent_rag` configuration declares enabled corpora, allowed
hosts/origins and bounded request/structured-result sizes.

Tools are `search_evidence(question, corpus_id="default")` and
`get_evidence_source(corpus_id, version_id, evidence_id)`. Both return structured
JSON and read corpus data. Search uses `retrieve_evidence` and existing embedding
adapters/ledger, returning an EvidencePack, content hash, runtime/embedding modes,
qualification and `verified_answer: false`; it never generates or verifies answers.
Source fetch uses `Store.get_evidence_item` with one snapshot checking corpus,
version, excerpt ID and document deletion, and returns a typed EvidenceItem.
No tools accept keys, URLs, paths, provider/mode/budget overrides or mutations.
Errors are safe MCP tool errors; consumers must inspect `isError`.

See [Agent RAG interface](agent-rag-interface.md) for configuration, client example,
authorization limitations and rollout requirements.
