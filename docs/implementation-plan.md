# RAG PoC Implementation Plan

Planning baseline · 5 October 2026 · Ready for implementation planning handoff

## 1 Outcome and decisions

Build an end-to-end, document-grounded question-answering PoC that retrieves evidence, drafts a cited answer, checks that exact answer, and releases it only when the configured verification policy passes. Demonstrate whether verification reduces unsupported answers while preserving useful, correct and complete responses within measured latency and cost.

All model inference uses external endpoints. The application and its database run on premises. Provider selection, endpoint URLs, keys, model identifiers and operational limits come from one validated configuration file. This is the planning specification for the next implementation step.

| Decision | Planning baseline |
|---|---|
| Storage | PostgreSQL with pgvector and native PostgreSQL full-text search |
| Retrieval | Exact cosine search plus lexical search, fused with reciprocal rank fusion |
| Generation and embeddings | Configured OpenAI-compatible endpoints |
| Verification baseline | A separately configured OpenAI-compatible chat endpoint |
| Specialist candidate | Hosted full Clef, behind an explicit native-protocol adapter |
| Compatibility default | Require OpenAI-compatible active profiles; native Clef is an opt-in configuration |
| FactCG and MiniCheck | Conditional comparison candidates; no dependency or local inference |
| Release policy | Structural checks, every answer block, whole-answer check, then at most one repair |
| Evaluation | Frozen development/test split, paired baselines, human-reviewed gold and fault injection |

The original diagram’s ingestion, retrieval, generation and verdict gate remain useful. A configuration registry replaces the model-serving registry. A dedicated hosted reranker is deferred until retrieval evaluation demonstrates a need. The first PoC uses PostgreSQL for corpus data, vectors, source versions, durable jobs and evaluation traces, avoiding a second persistence system.

Qdrant Server is open source and self-hostable under Apache 2.0; its hosted service is a separate offering. PostgreSQL plus pgvector is selected because it fits the requested on-premises deployment and lets this bounded PoC manage retrieval and application records together. Neither option eliminates operating costs. [4–6]

Success means that an answer is supported by the selected evidence under a measured policy. It does not establish that the source documents are correct, current or sufficient for real-world decisions. The latency claim in the supplied social post is not an acceptance criterion for this system.

## 1A Scope and application structure

The first implementation targets one operator, one English-language corpus and up to 100 documents or 10,000 active chunks. Accept UTF-8 text, Markdown and text-readable PDFs up to 20 MiB or 200 PDF pages per file. These are configurable admission limits, not measured capacity claims. Image-only PDFs, OCR, complex table reconstruction and multilingual retrieval are outside the initial evaluation scope. Parsing failures remain visible rather than silently reducing the indexed corpus. [10]

Use Python with FastAPI, Pydantic, httpx, psycopg, Alembic and pypdf. Serve a small HTML/JavaScript interface from the application. Pin compatible supported package versions and container digests during implementation; no untested version matrix is prescribed here. Use pytest for contract, integration and failure-path tests. This is a proposed implementation stack, not a benchmark result.

| Component | Responsibility |
|---|---|
| API and UI | Uploads, query runs, evidence display, evaluation controls and operator traces |
| Durable worker | Leased ingestion/query/evaluation jobs, bounded concurrency and restart recovery |
| PostgreSQL | Original source bytes, immutable versions, chunks, vectors, jobs and run records |
| Retrieval service | Corpus filters, exact vector search, lexical search, fusion and deduplication |
| Model ports | Embed, generate and verify interfaces with normalized results |
| Provider adapters | HTTP authentication, transport envelopes, capability checks and response validation |
| Gate and evaluator | Release decisions, repair limits, metrics, comparison reports and audit records |

A Docker Compose deployment contains the web application, worker and PostgreSQL. It contains no model-serving container or GPU requirement. A local mock endpoint supports deterministic offline tests; it returns fixtures and does not run a model. No runtime dependency may automatically download model weights or tokenizers.

Application code depends on role interfaces rather than vendor names. Adding another provider using a supported protocol requires configuration only. Adding a genuinely different protocol requires an adapter and its contract tests; configuration cannot erase protocol differences. Storage is accessed through a retrieval/store interface, but only PostgreSQL is implemented for the initial PoC.

The on-premises boundary covers application execution and stored data. Text selected for embedding, generation or verification is sent to the configured external endpoint. The run trace records which profile received each operation. This data flow follows directly from the requirement to avoid local inference.

## 2 Provider and configuration contract

One private YAML file is the source of configuration. It accepts direct API key values. Optional environment or secret-file references are supported when explicitly chosen; they are not required. Check in only a placeholder example. Resolve the configuration at startup, validate active roles, redact credentials in errors and logs, and record a sanitized configuration fingerprint with every run.

Each named profile declares a protocol, complete endpoint URL, authentication settings, request model identifier, capability flags, input/output limits, token-counting strategy, timeouts, retry policy, concurrency and dated pricing when cost enforcement is enabled. The code never infers a provider from the hostname, appends an assumed version path, or silently selects another provider after a failure.

| Contract | Required behavior |
|---|---|
| chat_completions | Configured complete chat endpoint; model and supported parameters only |
| embeddings | Configured complete embedding endpoint; validate each returned vector and index |
| cloudflare_clef | Explicit Workers AI URL, account identifier in the URL and native body model |
| Capabilities | Schema/JSON support, parameter names, token limits and score semantics are explicit |
| Active roles | Embedding, generator and verifier profiles must be complete and supported |
| Inactive profiles | May retain placeholders; they do not trigger network calls or startup failure |
| Authentication | Bearer is the default example; explicit header configuration covers other compatible services |

OpenAI-compatible does not mean that every endpoint supports identical parameters. The chat adapter must distinguish strict JSON schema, JSON-only output and locally validated text/JSON responses. Select max_tokens or max_completion_tokens and optional temperature support through capabilities. Handle refusals, incomplete generations and malformed output explicitly. A schema-valid answer can still be unsupported. [7–9]

Clef is documented as a hosted decision model with a Workers AI/SystemOne-style request. The full endpoint path contains @cf/cloudflare/clef, while the request body uses model: clef. Its documented interface supports 1–64 questions and a 65,536-token context. Cloudflare’s general OpenAI compatibility page does not establish that Clef accepts Chat Completions. Consequently, require_openai_compatible defaults to true and rejects an active Clef profile. Selecting native Clef requires an explicit false value and verifier-role change. [1–3]

Use full Clef as the first specialist comparison. Cloudflare’s own reported RAGTruth F1 is 79.4 for full Clef and 35.6 for Flash, which motivates testing full Clef first; these are vendor benchmark results, not our expected error rate. The current published input prices are $0.24 and $0.09 per million tokens respectively. Recheck availability and pricing before any paid run. [1–2]

## 2A Configuration example

This is a design example. Values marked REPLACE must be supplied for active profiles before a live run. Endpoint URLs include the complete operation path. Blank pricing and a zero study budget keep paid runs disabled. JSON capability values must describe the chosen endpoint accurately.

```yaml
config_version: 1
runtime:
  require_openai_compatible: true
  remote_concurrency: 4
  query_deadline_seconds: 60
  max_remote_attempts_per_query: 10
  mode: mock  # explicit change to live after configuration

budgets:
  total_max_estimated_cost_usd: 0
  phase_max_estimated_cost_usd:
    ingestion: 0
    queries: 0
    smoke: 0
    evaluation: 0

profiles:
  embedding_primary:
    protocol: embeddings
    endpoint: https://REPLACE.example/v1/embeddings
    api_key: REPLACE_IN_PRIVATE_CONFIG
    model: REPLACE_EMBEDDING_MODEL
    embedding_space: corpus-space-v1
    dimensions: null  # fill with the verified output dimension
    request_dimensions: false
    batch_size: 16
    max_input_tokens: null
    token_counting: conservative_utf8_bytes
    timeout_seconds: 30
    max_attempts: 2
    pricing: null

  chat_primary:
    protocol: chat_completions
    endpoint: https://REPLACE.example/v1/chat/completions
    api_key: REPLACE_IN_PRIVATE_CONFIG
    model: REPLACE_GENERATOR_MODEL
    capabilities:
      structured_output: json_schema
      output_limit_parameter: max_completion_tokens
      temperature: false
    max_input_tokens: null
    max_output_tokens: 1200
    token_counting: conservative_utf8_bytes
    timeout_seconds: 30
    max_attempts: 2
    pricing: null

```

The example deliberately leaves endpoint-specific dimensions and input limits unset. Active live profiles with missing required values fail validation. The mock profile resolves separate fixture settings and never treats placeholder hosts as real destinations. A model that cannot meet the declared contract is rejected rather than silently degraded.

## 2B Verification profiles and policy configuration

```yaml
  # Continue the same profiles mapping from the previous block.
  chat_verifier:
    protocol: chat_completions
    endpoint: https://REPLACE.example/v1/chat/completions
    api_key: REPLACE_IN_PRIVATE_CONFIG
    model: REPLACE_VERIFIER_MODEL
    capabilities:
      structured_output: json_schema
      output_limit_parameter: max_completion_tokens
      temperature: false
    max_input_tokens: null
    max_output_tokens: 1600
    token_counting: conservative_utf8_bytes
    timeout_seconds: 30
    max_attempts: 2
    pricing: null

  clef_full:
    protocol: cloudflare_clef
    endpoint: >-
      https://api.cloudflare.com/client/v4/accounts/REPLACE_ID/ai/run/@cf/cloudflare/clef
    api_key: REPLACE_IN_PRIVATE_CONFIG
    model: clef
    max_input_tokens: 65536
    max_questions: 64
    token_counting: conservative_utf8_bytes
    context_headroom_fraction: 0.25
    timeout_seconds: 30
    max_attempts: 2
    pricing:
      input_usd_per_million: 0.24
      output_usd_per_million: 0
      checked_on: '2026-10-05'

roles:
  embeddings: embedding_primary
  generator: chat_primary
  verifier: chat_verifier
verification:
  mode: shadow
  policy_id: null
  score_threshold: null
  max_answer_blocks: 8
  max_content_repairs: 1
  evidence_policy: frozen
retrieval:
  dense_candidates: 40
  lexical_candidates: 40
  rrf_constant: 60
  evidence_chunks: 8
  search_mode: exact
evaluation:
  max_remote_attempts: 3000
```

Concatenate both fragments into one YAML file. Clef requires require_openai_compatible: false and roles.verifier: clef_full. Shadow drafts remain unverified; evaluation tests a frozen policy; interactive gating requires passing held-out results. Native-score threshold selection does not itself calibrate probabilities.

## 3 Storage and ingestion

Use immutable source and processing records. The central entities are corpus, document, document_version, chunk, embedding_space, chunk_embedding, ingest_job, query_run, evidence_pack, verification_attempt and evaluation_run. Each run records the selected source versions, prompt/schema/policy versions, normalized configuration fingerprint and trace identifiers. Store original upload bytes in PostgreSQL for this bounded PoC so source previews and backup restoration remain reproducible.

Document identity and content identity are distinct. An upload hash detects duplicate bytes; another filename does not require another embedding job. A new version retains the previous immutable version. Chunk identity binds the document version, extraction/chunking version, text hash and source location. Preserve page references and character ranges so citations can open the exact evidence.

| Ingestion stage | Contract and completion condition |
|---|---|
| Admission | File type, size and page limits validated; duplicate handling is idempotent |
| Extraction | Per-page result recorded; unreadable content becomes needs_ocr or needs_review |
| Chunking | Paragraph/sentence-aware; target 1,600 characters, maximum 2,400, about 200 overlap |
| Embedding | Batches of at most 16 or a smaller provider limit; responses matched by returned index |
| Validation | Exactly one vector per input; expected dimension; finite values; nonzero norm |
| Activation | All required chunks/vectors complete; one short transaction publishes the version |

Characters are a chunking heuristic, not a token estimate. Preserve negation, units, dates and punctuation; use minimal Unicode normalization and retain original text. Track extraction coverage. A blank page is distinguishable from a failed page, and a mixed PDF with failed substantive pages is not silently declared ready. A human preview can resolve a review state before activation. PDF extraction does not reconstruct arbitrary layouts or perform OCR. [10]

The worker claims jobs with a lease, persists completed batches and resumes after restart. Fence completion writes with the current lease token so a late worker cannot publish after losing ownership. Source activation also checks the intended version sequence so an older job cannot replace a newer accepted version. Cache embeddings by exact input hash and embedding-space identity; ingestion idempotency also includes parser/chunker revision. Enforce both per-input and total embedding-batch budgets. Never hold a database transaction during remote inference. Failed jobs leave the prior active version available.

An embedding space fixes the semantic model identity/revision where available, output dimension, preprocessing and distance metric. Changing the embedding model creates a new space and requires re-embedding before activation. Key rotation and timeout changes do not. A changed endpoint is accepted within an existing space only when its declared semantic identity and a compatibility check agree; otherwise create a new space. Unpinned model aliases are recorded as a reproducibility limitation.

## 4 Retrieval and evidence snapshots

Begin with exact cosine retrieval, an exact distance ordering over eligible vectors rather than a guarantee of semantic relevance. PostgreSQL full-text search uses an explicit English configuration, positional tsvector data and ts_rank_cd; it is not BM25. Start with websearch_to_tsquery and trace empty/restrictive lexical results. Fuse the top 40 dense and top 40 lexical hits with reciprocal rank fusion, using constant 60. Apply chunk-ID tie-breaking and overlap deduplication, then select up to eight chunks subject to the downstream context budget. Tune defaults on development data. [4, 11]

For each candidate, the fused score is the sum of 1 / (60 + rank) over branches containing it. Keep dense, lexical and fused ranks separately. A lexical branch with no matches does not invalidate a usable dense branch. A database or embedding failure is a technical failure, not evidence that the corpus contains no answer.

| Step | Required invariant |
|---|---|
| Pin | Read corpus, active embedding-space ID and semantic configuration revision |
| Embed query | Call the selected profile outside a database transaction |
| Retrieve | Enter a short read-only repeatable-read transaction and recheck the pinned space |
| Handle activation race | If the space changed, abort/restart within the existing deadline and budget |
| Fuse | Dense and lexical branches read the same active-document snapshot and filters |
| Freeze | Persist selected immutable chunks, source versions, exact text and a content hash |
| Use | Generation, verification and repair use this frozen evidence pack |

Default read-committed isolation can give successive statements different snapshots. Use one SQL statement for both branches or a short repeatable-read transaction; the implementation baseline is the latter. Do not keep it open during generation or verification. A source update after retrieval cannot change an already frozen answer trace. [12]

Context packing includes the question, prompts, evidence, maximum draft, verdict questions and output reservations. If eight chunks do not fit, deterministically reduce the pack before drafting and log omissions. Once drafting begins, verification cannot substitute or truncate evidence. The smaller usable generator/verifier context constrains the shared pack. Apply identical corpus, version and embedding-space filters to both retrieval branches.

Only introduce HNSW after measurements show exact search misses the agreed latency target. Compare ANN results to exact search under the actual filters and examine recall. pgvector’s approximate index dimension limits differ from plain vector storage; validate the chosen embedding dimension before creating an index. Filtering can affect ANN result counts, so iterative scans are a tuning mechanism, not a proof of complete recall. [4]

The first experiment has no reranker. If required-evidence coverage misses its target, first inspect extraction, chunk boundaries, query language and fusion. A hosted reranker can then be a separately measured development experiment with its own adapter and budget.

## 5 Answer and verification contract

The generator returns a structured draft containing one to eight short, self-contained answer blocks. Each has a unique block_id, exact text and cited chunk IDs. A deterministic renderer produces the complete visible answer from those blocks. It does not append an unchecked model-written introduction or summary. Long or malformed drafts fail validation or receive one bounded format retry.

```json
{
  "blocks": [
    {
      "block_id": "b1",
      "text": "The document states the applicable condition.",
      "citation_ids": ["chunk_17"]
    }
  ]
}
```

Every exact block is checked against its cited evidence, including numbers, qualifications, conditions and combined assertions. Three checks examine the complete answer: global.task_scope, global.internal_consistency and global.counterevidence. The last passes only if the answer does not conceal, contradict or unjustifiably resolve applicable counterevidence; an accurately attributed explanation of disagreement can pass. There are at most eleven checks. A model’s claim to have extracted every important claim is not a coverage guarantee.

The verifier port returns one result per expected ID, with this canonical information:

| Field | Meaning |
|---|---|
| check kind and ID | block_support with a block ID, or one of the three reserved global IDs |
| support_status | supported or not_supported for a completed block-support check |
| check_status | pass or fail for a completed global check; support is not synonymous with relevance |
| reason | Optional on failed checks: contradicted, insufficient_evidence, conflicting_evidence or not_provided; absent on success |
| support_score | Native numeric support score or null; never an invented confidence value |
| raw_scores | Optional original label distribution and its declared semantics |
| execution_status | ok, invalid_response, incomplete_coverage, over_budget, timeout, provider_unavailable or budget_exhausted |
| Binding | App-supplied answer hash, evidence hash, policy version and request/attempt IDs |

A binary verifier need not distinguish contradiction from missing evidence. Chat and Clef choice questions can provide richer labels, but normalization must preserve the actual capability. A numeric score is not automatically a calibrated correctness probability. Chat labels without native scores remain scoreless. Store the raw response and normalized result with credential redaction and bounded retention settings.

Structural checks validate JSON, schema, unique IDs/types, exact verdict coverage, allowed citations, source ranges and quoted text. Reject duplicate JSON keys at parsing and duplicate IDs in result arrays. Bind hashes, round and check IDs in application state; a model echo is not proof. Reject over-eight-block drafts rather than dropping content. Validate the transport envelope, refusal and finish status. These checks cannot prove semantic support.

## 5A Release and repair policy

A gated run releases the answer only when structural checks pass, every block is supported, all three global checks pass, all checks complete successfully and the validated acceptance policy accepts any native score. The UI displays the evidence and policy. “Supported by retrieved sources” describes this evidence-bounded decision. The policy records model/profile, prompt/schema, packing rules, limits, checks, thresholds and evaluation artifact. Changing these semantic inputs invalidates release eligibility until re-evaluation; key rotation does not.

| Outcome | Application behavior |
|---|---|
| All checks pass | Release the exact checked answer with citations |
| Unsupported content | Allow at most one content repair, then check every block and the complete answer again |
| Unsupported after repair | Return a fixed evidence-based abstention; a conflict explanation is releasable only if the existing repair draft passes all checks |
| No usable evidence | Return a fixed insufficient-evidence response; do not invent an answer |
| Invalid/incomplete verification | Return verification_unavailable; never convert it to “the evidence is insufficient” |
| Timeout, provider failure or budget exhaustion | Preserve the technical status and a retriable operator explanation |
| Missing qualified policy | Shadow or candidate-policy evaluation only; no interactive gated-release status |

The initial repair experiment uses the same frozen evidence. Repair receives the original question, draft, evidence and production verifier feedback, never gold labels or human adjudication. It may remove or narrow unsupported claims and must regenerate a complete draft. The new answer hash invalidates prior verdicts. Recheck every block and global check, including unchanged text. Do not append an unchecked explanation. Retrieval expansion is a separate future experiment.

Content repair follows only a completed semantic rejection. Technical verification failure is retried within its request budget or returned as unavailable; it does not justify rewriting content. Allow at most one content repair and two attempts per logical request, including bounded format retries, within the common deadline/call budget. Never change judges merely to obtain a pass. No automatic fallback provider is enabled.

Treat documents, user questions and drafts as untrusted data inside verification requests. Delimit evidence, preserve source IDs, disable tool execution and instruct the verifier to judge support rather than obey embedded instructions. Test adversarial instructions in both documents and generated answers. These defenses are evaluated; prompt wording alone is not a guarantee.

Clef’s hosted documentation says long state may be truncated and does not document a returned truncation flag. Budget the full payload with headroom and test evidence near the end of the permitted pack. Exact local counting is used only with an explicitly available compatible tokenizer; otherwise a conservative UTF-8-byte bound and recorded estimation method are used. Server-side coverage cannot be proven from usage counts alone. Missing verdicts and known over-budget requests fail closed, while the residual risk of undisclosed server truncation remains documented. [1]

## 6 Runtime limits and observability

The baseline allows four concurrent outbound calls across the worker pool, a 60-second query deadline measured from acceptance, and at most ten outbound attempts per query. A normal path is query embedding, generation and one batched verification request; repair adds generation and verification. At two attempts per logical request this fits ten. The maximum eleven checks fit Clef’s question ceiling. Extra extraction, compression or query-rewrite calls are not part of this pipeline. A non-batching verifier requires a different explicit budget before activation.

Per-call timeouts default to 30 seconds and are clamped to the remaining query deadline. Retry only transient failures such as selected 429/5xx/network errors, with bounded backoff and Retry-After handling. Authentication, unsupported capabilities and known input-limit errors are not retried. Cancel queued work when the client or run is cancelled, and ignore late responses after the run becomes terminal. Timed-out attempts may still incur provider charges and count against the budget.

Reserve estimated cost atomically before each call, including maximum output and concurrent work; reconcile usage. The persisted ledger survives restarts. All live calls need known pricing and remaining positive total and phase caps: ingestion, queries, smoke or evaluation. Unknown usage stays conservatively reserved and labeled unknown. Count failures, retries, repairs and embeddings. Budget replenishment is explicit. Estimates do not replace provider billing controls. Retry only when backoff and execution fit the deadline.

| Recorded data | Why it is needed |
|---|---|
| Version/hash identifiers | Reproduce the source, prompts, policy, schema and configuration |
| Retrieval candidates and scores | Diagnose missing evidence and fusion effects |
| Exact frozen evidence and draft | Bind the checked content to the released answer |
| Raw and normalized verdicts | Audit score semantics, parsing and policy decisions |
| Stage timings and attempts | Measure queueing, retrieval, inference, repair and total latency |
| Usage, rates and budget state | Attribute cost and identify missing accounting |
| Terminal status | Keep factual abstention, cancellation and technical failure distinct |

Logs/traces redact keys and authorization headers. Render uploads and model content as escaped text or sanitized Markdown. The raw private config never enters a run. Default to a 5 GiB retained-payload admission quota, 30-day inactive-source/run retention and seven-day failed-staging cleanup; preserve references needed by retained runs. Monitor actual database/index/WAL disk usage separately. An explicit deletion path handles source bytes and historical evidence together. Quota exhaustion stops new work while preserving active retrieval.

The proposed interactive target is p95 at or below 20 seconds without repair and 40 seconds with repair at four concurrent requests, including queue time. The 60-second deadline is a hard functional limit. These are targets to measure using the selected endpoints, not promises derived from vendor microbenchmarks. Report cold and warm cache measurements separately.

## 7 API and operator workflow

Long-running operations return 202 with a durable job or run ID. The UI polls status or receives status-only events. Unverified answer text is never streamed as an accepted response. The server is the authority for run state, selected profiles and gate status; the browser receives no API keys.

| Route | Planned behavior |
|---|---|
| POST /api/documents | Upload source and return an ingestion job |
| GET /api/jobs/{id} | Read ingestion/evaluation progress and recoverable errors |
| GET /api/source-versions/{id} | Source metadata, extraction coverage and immutable preview |
| POST /api/retrieval/preview | Retrieve and display evidence with scores, without answer generation |
| POST /api/queries | Start a query against a selected corpus and policy |
| GET /api/runs/{id} | Status, released answer or abstention, citations and allowed trace data |
| POST /api/evaluations | Start a configured, budgeted evaluation suite |
| GET /api/evaluations/{id} | Metrics, progress, case outcomes and export links |
| GET /health/live and /health/ready | Process health and configuration/database readiness without paid inference |

The Documents view shows queued, processing, needs_review, needs_ocr, failed and ready states with version information. It offers extraction previews and an explicit retry after a recoverable ingestion failure. The Ask view shows the selected corpus, processing status, cited released answer, evidence-based abstention or technical failure. The Evaluations view shows variant comparisons, denominators, confidence intervals, latency/cost and failure examples.

Operator trace access is separate from normal answer presentation. It can show a shadow-mode or rejected draft only with a prominent unverified label. Source previews resolve to the exact immutable document version used by the answer. A simple corpus/version selector prevents ambiguity without building multi-tenant authorization or enterprise identity management into the initial PoC.

Query state is queued, retrieving, generating, verifying, repairing and then one terminal state: answered, abstained, verification_unavailable, failed, cancelled or timed_out. The UI preserves the difference between an abstention supported by missing/conflicting evidence and a service failure. The same run ID and stage counters survive worker restart; non-idempotent remote calls are not blindly replayed without recording a new attempt.

Expose planned command-line entry points for database migration, API/worker startup, ingestion, retrieval preview, offline contract tests, endpoint smoke tests and evaluation export. All accept --config PATH. The README explains the minimal operator sequence and gives a mock-mode walkthrough before live endpoints are configured.

## 8 Evaluation data and gold labels

Create a versioned evaluation set around the actual document domain. The proposed initial size is 30 source families containing roughly 90 documents. Keep revisions, near duplicates and related templates in the same family. Use ten families for development and twenty for held-out testing. Test documents are indexed for the final retrieval experiment, but their questions and labels are not used to tune prompts, thresholds, chunking or fusion.

| Split | Composition and purpose |
|---|---|
| Development | 100 questions from ten source families; tune prompts, policy and retrieval |
| Held-out answerable | 140 questions, including 40 requiring multiple evidence spans |
| Held-out missing evidence | 40 questions whose requested material facts are absent |
| Held-out conflict | 20 questions with unresolved, simultaneously applicable source conflicts |
| Controlled verifier development | 100 supported claims plus 100 reviewed unsupported mutations |
| Controlled verifier test | 200 supported claims plus 200 matched unsupported mutations |

For each question, record expected response type, sufficient gold evidence alternatives, required facts, qualifiers and prohibited conclusions. Define the source applicability of conflicts. Controlled claims record evidence, label and error type; originals and mutations stay together and inherit their evidence family’s split. The schema records family ID, split, annotator/adjudication state and dataset version.

The 200 held-out unsupported mutations contain 25 examples in each of eight categories: negation, number/unit, entity, date, condition, scope/quantifier, unsupported addition and misleading combination. Review every mutation for a natural phrasing and a genuinely changed support label. Synthetic mutations probe known failure modes; they do not estimate the prevalence of natural generation errors.

Two independent reviewers adjudicate question/evidence gold and controlled labels before execution. After execution, independently review every distinct initial/repaired answer, blind to variant and verifier verdict, for all material facts; reuse the adjudication of a shared draft. This review supplies natural-error metrics and the release audit. A hosted judge may assist triage but is not gold. With only one qualified reviewer, label results preliminary.

Label creation starts alongside the application work, using a written rubric and pilot agreement review. Do not assume an automated script can supply the domain expertise. If clinical documents are chosen, appropriate domain review is part of the evaluation work package; the PoC is not a clinical validation study. Dates and source validity must be represented explicitly so an older superseded statement is not mislabeled as an unresolved current conflict.

## 9 Experimental method

Freeze the corpus, versions, split, extraction/chunking, profiles, model IDs, prompts, schemas, repair rules and policy before the held-out run. Select native-score thresholds on development data, preserving score semantics; threshold selection is distinct from probability calibration. A scoreless chat verifier uses a label policy. Preselect D as the primary qualification variant and the candidate profile on development data. Additional held-out candidates are comparisons, not an opportunity to select a winner retrospectively.

| Variant | Comparison it enables |
|---|---|
| A Ungated RAG | Establish answer quality and unsupported content before a gate |
| B Structural gate | Measure the effect of schema/citation/coverage rules without semantic judgments |
| C Semantic gate | Add exact-block and whole-answer verification with no content repair |
| D Semantic gate plus repair | Measure useful recovery after one repair and a complete second verification |

Generate and cache one initial draft/evidence pack per question, then apply A–D to it. Universal decoding must recover answer text; undecodable output is a failure in every variant. Schema, citation integrity and structural coverage gates begin in B; semantic support and verdict coverage begin in C. C/D share initial verification; D adds repair. Report paired A-to-B, B-to-C and C-to-D differences and actual logical work. Replay timing is not live performance. Transport/deadline/budget controls apply universally.

Compare dense-only, lexical-only and fused retrieval using required-evidence coverage before generation. Tune on development data. If a reranker is later added, evaluate it as a separate predeclared variant. Compare chat verification and full Clef on identical cases if native Clef access is enabled. Flash and hosted FactCG/MiniCheck are optional additional studies with their own configuration and spend caps. Availability is checked at implementation time; no weights are downloaded to make a candidate run.

Report natural end-to-end errors separately from the balanced controlled-claim challenge. The controlled set has an artificial unsupported prevalence and cannot establish real user-facing risk by itself. For answer-level errors, include any materially unsupported factual statement in an otherwise mostly correct response. Correctness and completeness require the expected answer content, not merely absence of errors.

Run a predeclared twenty-question held-out repeatability sample three times per selected profile, report disagreement and never retune on it. Temperature does not establish determinism. Predeclare Wilson 95% intervals for descriptive proportions and paired source-family bootstrap intervals for differences, including controlled claims. Report counts and the correlation limitation. Use a one-sided exact binomial bound for zero events as an independence-qualified reference; a zero-event bootstrap is not zero uncertainty.

Before running, produce a dry-run call/token estimate and reserve the configured study budget. A single pass over 300 development/test questions needs at most 300 initial generation calls, 300 repairs, 600 answer-verification batches and 600 controlled-claim checks, before retries, embeddings or repeatability checks. This is an upper planning count, not prepaid usage; actual batching and failures are recorded. The sample 3,000-attempt cap may deliberately stop an expensive run and can be changed only in configuration before execution.

## 10 Metrics and acceptance gates

These are proposed targets, not results. Apply end-to-end gates to primary variant D and the preselected profile. Controlled metrics qualify its verifier on the original claim/evidence pairs with repair disabled. An engineering-complete PoC may falsify the hypothesis. Report misses without moving thresholds. Qualification requires complete gold and a frozen run; revised policies need new untouched test evidence.

| Metric | Denominator and proposed target |
|---|---|
| Required-evidence coverage at 8 | Sufficient evidence for all required facts / 140 answerable questions; at least 126; equivalent gold alternatives count |
| Controlled false acceptance | Unsupported controlled claims accepted / 200; no more than 5% |
| Controlled supported retention | Supported controlled claims accepted / 200; at least 90% |
| Correct and complete answers | Correct, complete substantive responses / 140; at least 105, and no more than seven fewer than ungated A |
| Missing-evidence handling | Correct insufficient-evidence responses / 40; at least 38 |
| Conflict handling | Correct abstention or verified conflict explanation / 20; at least 19 |
| Released-answer audit | At least 100 substantive releases; zero material unsupported claims across every final response on all 200 cases, including factual abstention/conflict explanations |
| Live completion | At least 198/200 attempts finish with an answer or valid abstention; technical failure, timeout and cancellation remain failures |
| Operational integrity | Every mandatory fault case passes; no bypass or silent fallback |
| Interactive latency | Proposed p95 at most 20 seconds without repair and 40 with repair, at four concurrent requests |

Report natural claim false acceptance, supported retention, answer-level unsupported-release rate, correctness/completeness, abstention, technical failures and latency. Never remove operational failures from attempted-case denominators, including controlled claims. The answerable denominator stays 140 despite abstention. Missing-evidence/conflict success depends on final text, not just a status flag. Boilerplate abstention is not a substantive release. The 100-release floor is a sample-size check; the 105-correct-answer gate already exceeds it.

The controlled false-acceptance and retention gates are empirical point-estimate targets; report uncertainty alongside them rather than calling them confidence guarantees. For zero observed errors among 100 released answers, the one-sided 95% binomial upper bound is about 2.95% under an independence assumption. Shared source families weaken that assumption. Report family structure and clustered comparisons; do not claim that zero observed errors proves zero future risk.

Report p50 and p95 for queueing, retrieval, generation, verification, repair and total time; show warm/cold cache and repaired/unrepaired requests separately. Report tokens, attempts and estimated USD per attempted question and per correct complete answer. An unknown usage field remains unknown. A budget-exhausted run is incomplete and cannot qualify by reporting only its easiest completed cases.

Choose a validated acceptance policy only if it provides a useful measured tradeoff. If none passes, the implementation deliverable is a reproducible negative result with case analysis and a specific next experiment. Production rollout is not part of this planning baseline.

## 11 Verification and failure testing

Use focused tests for the contracts and failure modes that could release unchecked content, corrupt retrieval or invalidate evaluation. Offline fixtures cover provider variations without spending tokens. PostgreSQL integration tests exercise actual transactions, vector handling and restart behavior. Live smoke tests validate only the configured endpoints after credentials, limits and a positive budget are provided.

| Area | Mandatory cases and expected invariant |
|---|---|
| Configuration | Missing active key/limit, invalid role, unsupported parameter, duplicate YAML key; fail explicitly |
| Protocol boundary | Strict-compatible mode rejects active Clef; inactive placeholders are harmless; no automatic fallback |
| Authentication/network | 401/403, 429, 5xx, timeout, late response and cancellation; bounded attempts and terminal status |
| Embeddings | Reordered indices accepted correctly; missing/duplicate indices, wrong dimensions, NaN/Inf and zero vectors rejected |
| Generation | Invalid JSON, schema mismatch, duplicate blocks, empty answer, refusal and length termination rejected |
| Verification coverage | Missing/duplicate/foreign verdict IDs, wrong source IDs and incomplete batch never release an answer |
| Grounding | Correct, contradicted, absent, conflicted, compound and qualifier-sensitive claims yield the intended policy behavior |
| Repair | Exactly one content repair; all content rechecked; unchanged text cannot retain stale verdicts |
| Input budget | Oversized prompt, small verifier context, suffix evidence and near-limit payload; no silent local truncation |
| Adversarial data | Instructions in documents and drafts do not bypass verification or invoke tools |
| Source/index state | Duplicate upload, partial embedding failure, restart, atomic version activation and model-space changes |
| Snapshot and lease races | Space activation never mixes vectors; expired workers and out-of-order ingestion cannot publish stale results |
| Retrieval | Empty lexical/dense branches, filtered corpus, overlapping chunks and deterministic ties |
| Trace and retention | No draft in the public answer before release; secrets/content safely rendered; hash, deletion and quota behavior consistent |
| Evaluation accounting | Failed/abstained cases stay in denominators; retries, repairs and unknown usage are counted |
| Recovery | Restore into a clean pinned PostgreSQL/pgvector environment with migration version; sources, vectors and traces resolve without remote inference |

Expand these groups into a named matrix of roughly 48 meaningful cases during implementation. The exact count is secondary to covering every invariant. Test technical failures separately from missing factual evidence. A fixture that merely repeats implementation logic is not evidence of correctness; include intentionally inconsistent payloads and observable end-to-end outcomes.

## 12 Implementation work packages

Sequence work around stable contracts, with evaluation labeling in parallel from the start. One implementation owner can execute the order below; separate contributors can own adapters, storage and evaluation after the shared schemas are agreed. No duration is promised before the repository and selected endpoints are available.

| Package | Concrete output and completion check |
|---|---|
| WP0 Contracts and scaffold | Repository layout, typed schemas, placeholder config, lockfile and mock endpoint; configuration round-trip and redaction work |
| WP1 Providers | Embedding/chat adapters and optional Clef adapter; offline success/failure fixtures pass with no vendor logic in core |
| WP2 Storage and ingestion | Migrations, Compose database, source preview, chunking, embedding jobs and atomic activation; restart and vector validation verified |
| WP3 Retrieval | Exact dense, native FTS, RRF, snapshot handling and evidence packs; retrieval baselines produce reproducible rankings |
| WP4 Answer flow | Structured generation, API/UI, run states and complete traces; mock end-to-end upload-to-answer works |
| WP5 Gate and repair | Block/whole-answer verification, policy lifecycle, one repair, technical outcomes and release protection; mandatory gate faults pass |
| WP6 Evaluation assets | Gold rubric, family split, reviewed cases, paired runner, metrics and export; denominators and dataset versions independently checked |
| WP7 Live contract checks | Operator-supplied profiles, keys, declared limits and budget; minimal configured endpoints pass smoke tests |
| WP8 Frozen experiment | Development acceptance policy frozen, held-out A–D run, comparison report, latency/cost and failure analysis completed |
| WP9 Handoff | Clean-environment startup, migration/backup/restore instructions, verified example walkthrough and final readiness report |

WP6 labeling starts during WP0 and continues independently of adapter/storage work. WP3 depends on storage and embedding contracts; WP4 depends on retrieval and chat contracts; WP5 depends on the draft schema and verifier contract. Live checks can start when each adapter is ready, but the frozen experiment waits for complete gold, versioned policy and the fault suite. Keep test labels inaccessible to development tuning.

The application’s intended module boundaries are config, domain, providers, ingestion, storage, retrieval, generation, verification, policy, api, worker and evaluation. Tests mirror interfaces and observable workflows. Prompt/schema versions are named assets. Schema migrations are explicit and reversible where practical. This organization is a proposed repository structure, not code already written.

The first implementation milestone is a deterministic mock-mode vertical slice: upload a fixture, index it in PostgreSQL, ask a question, display evidence, generate a fixture draft, gate it and record the result. The second replaces fixture responses with configured live endpoints. The third produces a frozen evidence-based comparison and a clear qualification decision.

## 13 Deliverables and readiness

The implementation handoff will contain source code, a dependency lockfile, Docker Compose, migrations, a placeholder configuration file, provider contract fixtures, versioned prompts/schemas/policies, the gold-label rubric and datasets, evaluation scripts and reports. Exports include machine-readable JSON/CSV and an operator-readable comparison with denominators, uncertainty, representative errors and endpoint/configuration provenance.

| Readiness level | Required evidence |
|---|---|
| Engineering complete | Clean mock-mode startup; complete upload-to-release flow; restart, snapshot, gate, budget and recovery tests pass |
| Live integration verified | Selected endpoints pass authenticated contract checks with recorded limits, usage and failures |
| Quality qualified | Reviewed gold and frozen held-out run complete; predeclared quality and operational gates met |
| Ready for a broader pilot | Separate decision based on the measured result, corpus needs and unresolved limitations |

Before implementation starts, the architecture is settled sufficiently to scaffold the repository and offline contracts. Runtime inputs still to supply are the actual corpus, embedding/generator/verifier endpoint URLs and model identifiers, credentials, endpoint limits/pricing and a study budget. Choosing native Clef is a configuration decision, not a reason to redesign the pipeline. These inputs do not prevent the offline implementation work from starting.

The current planning review checked the published Clef API and limitations, Cloudflare compatibility documentation, PostgreSQL/pgvector behavior and licenses, OpenAI response/embedding contracts and PDF extraction limitations. It also inspected the supplied architecture and social-post screenshots. No authenticated inference, application benchmark, database deployment or application test has been executed as part of this planning document.

The principal remaining empirical questions are whether the chosen embedding model retrieves all necessary evidence, whether a chat or Clef gate rejects natural unsupported claims without excessive abstention, whether repairs recover useful answers, and whether latency/cost meet the targets. Hosted access and response behavior must still be verified against the actual accounts. The evaluation is designed to answer these questions rather than assume favorable results.

FactCG and MiniCheck remain on hold as conditional hosted comparisons. A candidate is admitted only after its exact model/version, hosted endpoint, protocol, limits and score semantics are established. A paper result or downloadable model card is not a usable hosted API. The OpenAI-compatible baseline does not depend on either candidate or on Clef access.

OpenSearch remains a reasonable later open-source option if measured search requirements demand a different lexical/hybrid stack. It is not a second database in this PoC. PostgreSQL native full-text search, its ranking semantics and the chosen fusion baseline should first be evaluated on the actual corpus. [13]

## 14 Source register

Primary sources checked on 5 October 2026. Product availability, model aliases and pricing must be rechecked before live use. Numeric performance targets elsewhere in this document are proposed project criteria, except where a vendor benchmark is explicitly identified.

1. Cloudflare full Clef and Flash model contracts, input limits, truncation behavior and published prices

https://developers.cloudflare.com/workers-ai/models/clef/

https://developers.cloudflare.com/workers-ai/models/clef-flash/

2. Cloudflare launch and official model card, including its own Decision Index results

https://blog.cloudflare.com/clef-decision-models/

https://huggingface.co/Cloudflare/clef

3. Workers AI OpenAI compatibility and Clef release announcement

https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/

https://developers.cloudflare.com/changelog/post/2026-10-01-clef-workers-ai/

4. pgvector capabilities, search behavior and license

https://github.com/pgvector/pgvector

https://github.com/pgvector/pgvector/blob/master/LICENSE

5. PostgreSQL license

https://www.postgresql.org/about/licence/

6. Qdrant Server source and license

https://github.com/qdrant/qdrant

7. OpenAI Chat Completions reference

https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create

8. OpenAI structured outputs guide

https://developers.openai.com/api/docs/guides/structured-outputs

9. OpenAI embeddings guide

https://developers.openai.com/api/docs/guides/embeddings

10. pypdf text extraction documentation

https://pypdf.readthedocs.io/en/stable/user/extract-text.html

11. PostgreSQL full-text ranking and query controls

https://www.postgresql.org/docs/current/textsearch-controls.html

12. PostgreSQL transaction isolation

https://www.postgresql.org/docs/current/transaction-iso.html

13. OpenSearch project and hybrid search documentation

https://opensearch.org/about/

https://docs.opensearch.org/latest/vector-search/ai-search/hybrid-search/index/
