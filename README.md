# Evidence Lab

Document-grounded question answering built on **LangGraph**, standard **LangChain model/tool interfaces**, PostgreSQL conversation memory and optional **LangSmith** tracing. Answers pass citation checks, verification and one optional repair before release.

The default demo uses deterministic `fixture_only` answers and makes no inference requests. No models or tokenizers are downloaded or hosted.

See the [documentation index](docs/README.md), [approved design](docs/implementation-plan.md) and [Cloudflare Clef activation](#cloudflare-clef-verifier).

## Names and workspace layout

Names: CLI/distribution `evidence-lab`, Python package `evidence_lab`, frontend `@evidence-lab/dashboard`, Compose project `evidence-lab`, image `evidence-lab:local`. The checkout may remain `rag-poc`. Fresh deployments use `evidence_*` tables and `EVIDENCE_LAB_*` environment variables; legacy database/environment compatibility is removed.

```text
.
├── apps/
│   ├── evidence-lab/           # Python API, worker, migrations and tests
│   └── dashboard/              # Vue 3 + TypeScript application and tests
│       ├── src/{views,components,composables,api,types,utils,assets}/
│       ├── public/             # Editable favicon/static inputs
│       └── dist/               # Generated Vite output (ignored)
├── tooling/                    # Development, backup/restore and cleanup helpers
├── configs/                    # Mock/Compose samples and private live template
├── data/                       # Synthetic fixture documents and datasets
├── docs/                       # Contracts, evaluation and experiment guides
├── .agents/{skills,agents}/    # Project workflows and agent role definitions
├── .codex/agents               # Link to canonical project agent definitions
├── Taskfile.yml                # Root Task runner; app Taskfiles live under apps/
├── pyproject.toml / uv.lock     # Root Python workspace and lock
└── pnpm-workspace.yaml / pnpm-lock.yaml  # Frontend workspace and lock
```

Vue builds into `apps/dashboard/dist/`. The old Python static UI, source manifest and verification script are removed; Git tracks source history and Task runs checks.

## Start the complete demo

Requirements: Task and Docker with Compose. Run these commands from the repository root:

```bash
task compose:up
task compose:seed
```

Open **http://127.0.0.1:8000** for Documents, Ask and Evaluations.

Compose runs PostgreSQL/pgvector, migrations, API and worker on localhost. Sample DB credentials: `evidence:evidence`. Pinned images are recorded in [container-images.json](docs/container-images.json).

Upload a text file containing:

```text
The Atlas refund period is 30 days after purchase.
Refund requests require the order identifier.
```

Ask `What is the Atlas refund period in days?` and open its citation. Mock markers: `[fixture:unsupported]` exercises repair; `[fixture:conflict]` exercises abstention.

`task compose:stop` stops services and preserves database data.

## Local development

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), [Task](https://taskfile.dev/), Node.js 24+, [pnpm](https://pnpm.io/) and Docker Compose for the default database.

```bash
task setup
```

Start the API, worker and Vue hot reload in one terminal:

```bash
task dev
```

Open `http://127.0.0.1:5173/static/`. The default mock configuration starts PostgreSQL if needed and applies migrations. Ctrl+C stops API, worker and Vite; the database stays running. Restart after Python changes.

For custom configuration, prepare PostgreSQL and run `task app:migrate CONFIG=...` first. Use `task dev CONFIG=configs/private.yaml -- --dashboard-port 5174` to select configuration and frontend port.

Seed demo documents with `task app:seed`. For the compiled dashboard, run `task dashboard:serve` and `task app:worker` separately after preparing the database.

Workers default to four concurrent jobs. PostgreSQL leases and reservations enforce publication, spending and outbound-call limits across restarts.

## Architecture and data flow

The API admits work; the worker executes durable jobs. PostgreSQL holds shared state, and provider adapters handle all inference. Retrieval preview runs directly through the API.

```mermaid
flowchart LR
  subgraph Dashboard["Vue dashboard: apps/dashboard/src"]
    Views["App.vue, views and components"] --> State["useDashboard and typed context"]
    State --> Client["api/client.ts and types/api.ts"]
  end
  Client --> API["api.py: HTTP admission and public results"]
  CLI["cli.py: operator commands"] --> Store["storage.py: PostgreSQL / pgvector"]
  API --> Store
  API --> Retrieval["retrieval.py: hybrid evidence"]
  Store --> Worker["worker.py: claim and renew leases"]
  Worker --> Ingestion["ingestion.py: extract, chunk, embed"]
  Worker --> Engine["engine.py: LangGraph orchestration"]
  Worker --> Eval["evaluation.py: paired studies"]
  Engine --> Retrieval
  Engine --> Policy["policy.py: structural checks and release gate"]
  Engine --> Hub["providers: embedding, generation, verification"]
  Ingestion --> Hub
  Retrieval --> Hub
  Eval --> Hub
  Ingestion --> Store
  Retrieval --> Store
  Engine --> Store
  Eval --> Store
  Hub --> Store
  Hub --> Inference["Deterministic fixtures OR external inference"]
```

`domain.py` defines contracts; `config.py` validates settings/secrets; `experiments.py` runs load and repeatability studies. PostgreSQL fences worker writes and reserves provider costs before transport.

### Development and container serving

| Address (default) | Service | What it serves |
|---|---|---|
| `http://127.0.0.1:5173/static/` | Vite, started by `task dev` | Vue source with hot reload; `/api` and `/health` proxy to the configured API |
| `http://127.0.0.1:8000/` | FastAPI | Compiled dashboard from `apps/dashboard/dist/`, plus `/api/*` and health endpoints |
| `127.0.0.1:5432` | PostgreSQL/pgvector | Durable documents, versions, vectors, jobs, runs, traces and budget ledger |

Both dashboard addresses use the same backend/database. Use 5173 for frontend hot reload. Run only one API on port 8000.

```mermaid
flowchart LR
  Browser["Browser"] --> Vite["Development: Vite :5173/static/"]
  Vite -->|"/api and /health proxy"| API["FastAPI :8000"]
  Browser -->|"Compiled dashboard and API on one origin"| API
  Build["Vue + TypeScript → Vite build"] --> Assets["apps/dashboard/dist"]
  Assets --> API
  API --> DB["PostgreSQL :5432"]
  Worker["Durable worker"] --> DB
  Worker --> Providers["Configured inference adapters"]
```

Compose finishes migrations before starting API/worker. `task dev` prepares the default mock database before starting local services.

### Document ingestion

```mermaid
sequenceDiagram
  participant UI as Dashboard
  participant API as API admission
  participant DB as PostgreSQL
  participant W as Worker
  participant P as Embedding adapter
  UI->>API: Upload text, Markdown or PDF
  API->>DB: Store immutable bytes/version and enqueue ingestion
  API-->>UI: 202 with document, version and job IDs
  W->>DB: Claim job with fenced lease
  W->>W: Extract pages and stable chunks
  W->>DB: Read embedding cache and save staged extraction
  W->>P: Embed missing chunk text within limits
  P-->>W: Validated vectors
  W->>DB: Save vectors and activate complete version
  UI->>API: Poll job and document state
  API->>DB: Read durable processing result
  API-->>UI: Ready, needs_review or failure state
```

Uploads create immutable versions; only complete vectors activate them. Ambiguous PDFs stay `needs_review`. CLI `ingest --wait` processes jobs locally.

### Query and answer release

```mermaid
flowchart TD
  Q["Accept question → durable run/job"] --> R["Embed query; exact dense + lexical retrieval"]
  R --> F["RRF fusion, overlap deduplication, frozen evidence pack"]
  F --> E{"Evidence available?"}
  E -->|No| A["Abstain: insufficient evidence"]
  E -->|Yes| D["Generate structured cited draft"]
  D --> S{"Citation and quotation checks pass?"}
  S -->|No| T["Technical failure; no public draft"]
  S -->|Yes| V["Verify every block + all global checks"]
  V --> C{"Complete valid verdict?"}
  C -->|No| T
  C -->|Yes| G{"Semantic checks accepted?"}
  G -->|No| Repair{"Repair still allowed?"}
  Repair -->|Yes: at most once| Fix["Generate complete replacement using same evidence"]
  Fix --> S
  Repair -->|No| A2["Abstain: unsupported after verification"]
  G -->|Yes| P{"Release policy allows answer?"}
  P -->|Yes| Answer["Publish exact checked answer and citations"]
  P -->|No| Shadow["Shadow result; candidate only in operator trace"]
```

Deadlines include queue time. Failures never publish drafts; repairs repeat every check against the same frozen evidence. Live gated mode requires qualification before inference.

### Evaluation and qualification

```mermaid
flowchart LR
  Dataset["Dataset + frozen evidence/initial draft"] --> ABCD["Paired A/B/C/D evaluation"]
  ABCD --> Report["Raw report + annotation template"]
  Humans["Independent human review"] --> Reviewed["Hash-bound reviewed report"]
  Report --> Reviewed
  Faults["Externally collected fault study"] --> Qualify["qualify: assess complete evidence"]
  Load["Independent live-load study"] --> Qualify
  Repeat["Repeatability study"] --> Qualify
  Reviewed --> Qualify
  Qualify --> Policy["Qualified policy bound to implementation and settings"]
  Policy --> Gate["Live gated release"]
```

Fixtures remain unqualified. See [evaluation](docs/evaluation.md), [experiments](docs/experiments.md) and [test commands](#tests-recovery-and-operations).

## LangGraph showcase

The query graph invokes a corpus-scoped retrieval tool, a structured LangChain chat model and a scoped verification tool (native Clef or the configured chat verifier). The dashboard shows stage completion, repair executions and timings. Provider adapters retain exact endpoint contracts, bounded retries and PostgreSQL spending reservations.

OpenAI-compatible calls use `ChatOpenAI` and `OpenAIEmbeddings` through a single-use HTTP guard that reserves spending in PostgreSQL before sending. Native Clef retains its adapter. Prompts, strict schemas and structured-output composition also use LangChain built-ins. See [provider integrations](apps/evidence-lab/src/evidence_lab/providers/README.md) for the reuse map and custom wire/accounting boundaries.

**Memory:** each conversation belongs to one corpus. Follow-ups receive a bounded snapshot of previously released answers; prior dialogue is context, never evidence. “New conversation” clears the thread selection. Each query retrieves fresh evidence. Source deletion clears copied dialogue and cancels pending affected work.

**LangSmith:** explicitly enable metadata export in private YAML:

```yaml
langsmith:
  enabled: true
  project: evidence-lab
  api_url: https://api.smith.langchain.com
  api_key_env: EVIDENCE_LAB_LANGSMITH_API_KEY
  timeout_seconds: 2
```

Set the selected key in your environment, then restart API/worker. Direct `api_key` also works. Exported traces contain run IDs, node status and timings; prompts, source text, drafts, dialogue and credentials are excluded. Ambient `LANGSMITH_TRACING` cannot enable content export. Export outages do not change query results.

Conversation memory is durable PostgreSQL state. Graph stage checkpoints are not enabled: interrupted nonterminal jobs restart under the existing lease/attempt ledger. This is an enterprise-oriented showcase, not a production-readiness certification.

## Configure your providers

Copy the live template and replace placeholders for `embeddings`, `generator` and `verifier`:

```bash
cp configs/live.example.yaml configs/private.yaml
task app:cli -- config-check --config configs/private.yaml
```

Credentials come from private YAML: choose `api_key`, `api_key_env` or `api_key_file`. Logs and public configuration redact secrets and the database DSN.

Each profile specifies:

| Setting | Meaning |
|---|---|
| `protocol` | `embeddings`, `chat_completions`, or explicitly enabled `cloudflare_clef` |
| `endpoint` | Complete operation URL, including its path; the code appends nothing |
| `api_key`, authentication header/prefix | Credentials and wire authentication |
| `model`, optional `semantic_revision` | The request model identifier and an operator-tracked revision |
| `capabilities` | JSON schema / JSON object / text JSON, supported output-limit parameter, temperature support |
| Limits | Input/output limits, batch dimensions, timeout, attempts and concurrency |
| `pricing` | Declared input/output USD per million tokens and `checked_on` date |

Protocols and capabilities are explicit; changing wire protocols requires an adapter. There is no automatic provider fallback.

Start live setup in `verification.mode: shadow`. Candidates remain in the unverified operator trace; gated answers require a matching qualified policy.

Live calls require declared prices and positive global/phase budgets. Zero disables spending. Retries and repairs reserve cost before transport.

After configuring endpoints and the smoke budget:

```bash
task app:cli -- smoke --config configs/private.yaml
task app:cli -- smoke --config configs/private.yaml --execute
```

The first command plans calls; `--execute` sends embedding, generation and verification requests.

## Cloudflare Clef verifier

**Clef is supported through a native `cloudflare_clef` adapter.** It checks answer blocks and global consistency against frozen evidence; your configured chat model still generates answers. See [Cloudflare's Clef reference](https://developers.cloudflare.com/workers-ai/models/clef/).

### Activate Clef

1. Copy the complete [clef.example.yaml](configs/clef.example.yaml) to `configs/private.yaml` for OpenAI embeddings/generation plus Clef. See [configuration setup](configs/README.md) for the exact substitutions. For other providers, start from `configs/live.example.yaml`.
2. Get your Cloudflare account ID and a [Workers AI API token](https://developers.cloudflare.com/workers-ai/get-started/rest-api/).
3. Merge these settings into the private file, replacing the account/token placeholders:

```yaml
runtime:
  mode: live
  require_openai_compatible: false
roles:
  embeddings: embedding_primary
  generator: chat_primary
  verifier: clef_full
verification:
  mode: shadow
profiles:
  clef_full:
    protocol: cloudflare_clef
    endpoint: https://api.cloudflare.com/client/v4/accounts/REPLACE_ACCOUNT_ID/ai/run/@cf/cloudflare/clef
    api_key: REPLACE_API_TOKEN
    model: clef
    max_input_tokens: 65536
    max_questions: 64
    context_headroom_fraction: 0.25
    timeout_seconds: 30
    max_attempts: 2
    concurrency: 4
    pricing:
      input_usd_per_million: 0.24
      output_usd_per_million: 0
      checked_on: '2026-10-06'
```

The endpoint uses `@cf/cloudflare/clef`; the request body's model is `clef`. Omit `max_output_tokens` and chat `capabilities`. Recheck [current pricing](https://developers.cloudflare.com/workers-ai/models/clef/) before spending. You can substitute one `api_key_env` or `api_key_file` reference for the direct token.

4. Set positive `budgets.total_max_estimated_cost_usd` and phase caps for the operations you will run (`smoke`, `ingestion`, `queries`, `evaluation`). Zero disables that phase. Every active profile needs dated pricing.
5. Validate and preview the smoke check:

```bash
task app:cli CONFIG=configs/private.yaml -- config-check
task app:cli CONFIG=configs/private.yaml -- smoke
# Makes budgeted external calls to embeddings, generator and Clef:
task app:cli CONFIG=configs/private.yaml -- smoke --execute
```

6. Prepare the configured PostgreSQL database, then start the app:

```bash
task app:migrate CONFIG=configs/private.yaml
task dev CONFIG=configs/private.yaml
```

Use a new corpus when switching from fixture embeddings. In `shadow` mode, checked candidates appear only in the operator trace; public answers require a matching qualification artifact and `verification.mode: gated`. Restart API/worker after changing verifier settings. FactCG and MiniCheck remain deferred.

## Move from mock to live storage

After changing the embedding space, create a new corpus and re-upload sources. Existing vectors cannot be mixed with the new space. Compose database hostname: `db`.

To run a private configuration in Compose:

```bash
EVIDENCE_LAB_CONFIG=./configs/private.yaml task compose:up
```

Restart API/worker after configuration changes. For a separate project, free the ports and use `docker compose -p evidence-lab-live ...`.

The schema is a fresh baseline; existing legacy databases must be reset before using this version. Compose database credentials and private DSNs must match.

Containers run as UID `10001`; mounted YAML must be readable. Mount `api_key_file` read-only or explicitly forward the selected `api_key_env` variable.

## Configuration reference

Schema: [config.py](apps/evidence-lab/src/evidence_lab/config.py). Select YAML with Task `CONFIG=...` or CLI `--config`. Samples: `mock.yaml` (local), `mock.compose.yaml` (DB host `db`), `live.example.yaml` (incomplete live template), `clef.example.yaml` (complete OpenAI/Clef template requiring credentials). Tables show schema defaults; samples may override them.

Strict UTF-8 YAML, ≤1 MiB. Unknown/duplicate keys, wrong types and non-finite values fail validation. Required fields have no default; `null` means unset.

| Root field | Default | Meaning |
| --- | --- | --- |
| `config_version` | `1` | Only schema version `1` is supported. |
| `runtime`, `database`, `ingestion`, `retrieval`, `verification`, `budgets`, `evaluation`, `memory`, `langsmith` | Section defaults below | Optional sections use their schema defaults. |
| `profiles` | Required | Nonempty mapping of named provider contracts. Names start with a letter or number and contain up to 96 letters, numbers, `.`, `_` or `-`. |
| `roles` | Required | Assigns pipeline roles to profiles. |

### Runtime and database

| YAML field | Default | Meaning and constraints |
| --- | --- | --- |
| `runtime.mode` | `mock` | `mock` uses deterministic fixtures without remote inference or model credential resolution; `live` uses configured remote providers. Explicit LangSmith tracing is configured separately. |
| `runtime.require_openai_compatible` | `true` | Active native `cloudflare_clef` profiles require `false`. |
| `runtime.remote_concurrency` | `4` | Global remote-call concurrency / default worker concurrency; integer 1–64. |
| `runtime.query_deadline_seconds` | `60` | Query deadline in seconds; >0, ≤3600. |
| `runtime.max_remote_attempts_per_query` | `10` | Total provider-attempt allowance per query; 1–100. |
| `runtime.ingestion_deadline_seconds` | `1800` | Ingestion deadline in seconds; >0, ≤86400. |
| `runtime.max_remote_attempts_per_ingestion` | `2000` | Provider-attempt allowance per ingestion; 1–100000. |
| `runtime.host` | `127.0.0.1` | API bind address; the serve command can override it. |
| `runtime.port` | `8000` | API TCP port; 1–65535; the serve command can override it. |
| `runtime.worker_lease_seconds` | `120` | Job lease duration in seconds; 10–3600; worker heartbeats renew leases. |
| `runtime.poll_seconds` | `0.5` | Worker polling interval in seconds; >0, ≤60. |
| `runtime.operator_token` | `null` | Optional secret; when nonempty, `/api/` requests require `Authorization: Bearer <token>`. Health and dashboard assets remain accessible. Enter it in the dashboard's operator-token control. |
| `database.dsn` | `postgresql://evidence:evidence@localhost:5432/evidence_lab` | PostgreSQL URL with a host and database name; accepts `postgresql` or `postgres`. Credentials are redacted from public configuration. No production storage fallback. |

### Ingestion and retention

| YAML field | Default | Meaning and constraints |
| --- | --- | --- |
| `ingestion.max_upload_bytes` | `20971520` (20 MiB) | Maximum original file size; ≥1 byte. |
| `ingestion.max_pdf_pages` | `200` | Maximum PDF page count; ≥1. |
| `ingestion.max_documents` | `100` | Document quota; ≥1. |
| `ingestion.max_active_chunks` | `10000` | Active chunk quota; ≥1. |
| `ingestion.max_retained_payload_bytes` | `5368709120` (5 GiB) | Retained original-payload quota; ≥1 byte. |
| `ingestion.inactive_retention_days` | `30` | Age threshold for inactive-version retention cleanup; ≥1 day. |
| `ingestion.failed_staging_retention_days` | `7` | Age threshold for failed-staging cleanup; ≥1 day. |
| `ingestion.max_extracted_chars` | `10000000` | Extracted document text limit; ≥1 character. |
| `ingestion.max_page_extracted_chars` | `1000000` | Per-page extraction limit; ≥1 and no larger than document limit. |
| `ingestion.chunk_target_chars` | `1600` | Target chunk length; ≥1 character. |
| `ingestion.chunk_max_chars` | `2400` | Maximum chunk length; ≥ target length. |
| `ingestion.chunk_overlap_chars` | `200` | Chunk overlap; ≥0 and strictly smaller than target length. |
| `ingestion.embedding_batch_size` | `16` | Ingestion batch size; 1–16, additionally bounded by the embedding profile's batch size and context allowance. |

Retention runs through explicit CLI `cleanup`; it is not scheduled automatically.

### Retrieval and answer verification

| YAML field | Default | Meaning and constraints |
| --- | --- | --- |
| `retrieval.dense_candidates` | `40` | Vector-search candidate count; 0–1000. |
| `retrieval.lexical_candidates` | `40` | Full-text candidate count; 0–1000. At least one retrieval branch must have a positive count. |
| `retrieval.rrf_constant` | `60` | Reciprocal rank fusion constant; integer ≥1. |
| `retrieval.evidence_chunks` | `8` | Maximum selected evidence chunks; 1–64, further constrained by context packing. |
| `retrieval.search_mode` | `exact` | Only exact vector search is supported. |
| `verification.mode` | `shadow` | `shadow`, `evaluation` or `gated`; see the answer-release flow. |
| `verification.policy_id` | `null` | Identifier of the selected qualification policy. |
| `verification.policy_path` | `null` | Path to its qualification artifact. Setting an ID/path alone does not qualify a live gate. |
| `verification.score_threshold` | `null` | Optional scalar verifier threshold; 0–1. |
| `verification.max_answer_blocks` | `8` | Answer block limit; 1–8. |
| `verification.max_answer_bytes` | `8000` | Answer size limit in bytes; 512–64000. |
| `verification.max_repair_bytes` | `16384` | Repair payload size limit in bytes; 512–128000. |
| `verification.max_content_repairs` | `1` | Content repair count; 0 or 1. |
| `verification.evidence_policy` | `frozen` | Only frozen evidence is supported; repair uses the original evidence pack. |

### Budgets and evaluation

| YAML field | Default | Meaning and constraints |
| --- | --- | --- |
| `budgets.total_max_estimated_cost_usd` | `0` | Global estimated spending cap in USD; ≥0. Zero disables live calls. |
| `budgets.phase_max_estimated_cost_usd.ingestion` | `0` | Ingestion estimated spending cap in USD; ≥0. |
| `budgets.phase_max_estimated_cost_usd.queries` | `0` | Query estimated spending cap in USD; ≥0. |
| `budgets.phase_max_estimated_cost_usd.smoke` | `0` | Smoke-call estimated spending cap in USD; ≥0. |
| `budgets.phase_max_estimated_cost_usd.evaluation` | `0` | Evaluation estimated spending cap in USD; ≥0. Omitted phase entries default to zero; only these four keys are accepted. |
| `evaluation.max_remote_attempts` | `3000` | Evaluation provider-attempt allowance; integer ≥1. |
| `evaluation.dataset_dir` | `data` | Schema setting retained for dataset organization; current loading uses explicit CLI dataset paths or the bundled-demo resolver, rather than this field. |
| `evaluation.allowlisted_datasets` | `{demo: data/demo/dataset.json}` | Named dataset mapping. HTTP evaluation currently accepts only bundled `demo` in mock mode; the configured demo path is a fallback when repository demo assets are absent. |
| `evaluation.gold_path` | `null` | Human-review annotation path used when an explicit evaluation annotations argument is absent. |
| `evaluation.policy_output_dir` | `policies` | Schema setting retained for policy organization; current policy output is selected through explicit CLI output paths. |

Live spending requires positive global and phase caps plus dated active-profile prices. Unknown costs retain conservative reservations.

### Role assignments

| YAML field | Default | Supported profile protocol |
| --- | --- | --- |
| `roles.embeddings` | Required | `embeddings` |
| `roles.generator` | Required | `chat_completions` |
| `roles.verifier` | Required | `chat_completions` or `cloudflare_clef` |
| `roles.repair_generator` | `null` | `chat_completions`; unset uses the generator profile. |
| `roles.evaluation_judge` | `null` | `chat_completions` or `cloudflare_clef`; configure when a judge is requested. |

Role names must exist in `profiles`. Inactive profiles undergo schema validation but do not resolve secrets or require completed live endpoints.

### Provider profiles

Replace `<name>` with a profile key.

| YAML field under `profiles.<name>` | Default | Meaning and constraints |
| --- | --- | --- |
| `protocol` | Required | `embeddings`, `chat_completions` or native `cloudflare_clef`. |
| `endpoint` | `null` | Complete operation URL; no path is appended. Active live profiles require HTTP(S), host and non-root operation path, without URL credentials, query strings, fragments or placeholders. |
| `api_key` | `null` | Direct secret in private YAML; supported without environment variables. |
| `api_key_env` | `null` | Name of an explicitly selected environment variable containing the key. |
| `api_key_file` | `null` | UTF-8 secret file, ≤64 KiB; relative paths resolve from the configuration file's directory, `~` expands, surrounding whitespace is stripped. |
| `auth_header` | `Authorization` | Valid HTTP authentication header name. |
| `auth_prefix` | `Bearer` | Printable ASCII prefix; empty string sends the key without a prefix. |
| `model` | `null` | Provider model identifier; required for active embedding/live profiles. Native Clef accepts only `clef` or `clef-flash`. |
| `semantic_revision` | `null` | Operator-declared semantic model revision for embedding provenance and policy compatibility; unset revisions are reported as unpinned. |
| `embedding_space` | `null` | Embedding-space identifier; required for active embeddings. |
| `dimensions` | `null` | Embedding vector dimensions; integer 1–16000; required for active embeddings. |
| `request_dimensions` | `false` | Include the configured dimensions in embedding request bodies when supported by the endpoint. |
| `batch_size` | `16` | Profile batch ceiling; 1–2048. Ingestion also enforces its 1–16 application ceiling. |
| `capabilities` | `null` | Explicit chat request capabilities below; required for active chat profiles. |
| `max_input_tokens` | `null` | Declared context allowance; integer ≥1; required for every active profile. Native Clef ≤65536. |
| `max_batch_input_tokens` | `null` | Aggregate embedding-batch allowance; integer ≥1; defaults effectively to `max_input_tokens`. |
| `max_output_tokens` | `null` | Chat output reservation; integer ≥1 and smaller than usable input allowance; required for active chat, unsupported by native Clef. |
| `max_questions` | `64` | Native verification question batch ceiling; 1–64. An active native verifier must fit `max_answer_blocks + 3` checks. |
| `token_counting` | `conservative_utf8_bytes` | Only this conservative bound is supported: serialized UTF-8 bytes plus 64 framing units, without downloaded tokenizers. |
| `context_headroom_fraction` | `0.15` | Reserved context fraction; ≥0 and <1; usable input is `floor(max_input_tokens × (1 − fraction))`, before chat output/application reserves. |
| `timeout_seconds` | `30` | Per-attempt timeout; >0, ≤3600 seconds, also bounded by the job deadline. |
| `max_attempts` | `2` | Per-operation provider attempts; 1 or 2, also bounded by job attempt limits. |
| `concurrency` | `4` | Profile remote concurrency ceiling; 1–64; the global ceiling also applies. |
| `pricing` | `null` | Dated pricing below; required on all active live profiles whenever any spending cap is nonzero. |
| `capabilities.structured_output` | `json_schema` | `json_schema`, `json_object` or `text_json`; explicit chat capability declaration. |
| `capabilities.output_limit_parameter` | `max_completion_tokens` | `max_completion_tokens` or `max_tokens`, matching provider support. |
| `capabilities.temperature` | `false` | Whether the endpoint accepts a temperature parameter; `true` sends `temperature: 0`. This is a capability flag, not a temperature value. |
| `pricing.input_usd_per_million` | Required | Nonnegative USD per million input tokens. |
| `pricing.output_usd_per_million` | `0` | Nonnegative USD per million output tokens. |
| `pricing.checked_on` | Required | Quoted ISO date string `YYYY-MM-DD`; record when the operator checked the price. |

Active live profiles require exactly one credential source and a nonempty printable ASCII key. Mock mode ignores model credential references; explicitly enabled LangSmith resolves its selected tracing key. `api_key_file` resolves relative to the YAML file; dataset, policy and output paths resolve from the working directory.

### Conversation memory and LangSmith

| YAML field | Default | Meaning and constraints |
|---|---|---|
| `memory.enabled` | `true` | Include previously released turns in the conversation's acceptance-time snapshot |
| `memory.max_turns` | `6` | Retained complete question/answer pairs; 1–20 |
| `memory.max_context_bytes` | `8000` | Serialized dialogue byte cap; 256–32000; bundled samples choose 4000 |
| `langsmith.enabled` | `false` | Explicitly enable workflow metadata export |
| `langsmith.project` | `evidence-lab` | Trace project name; 1–96 characters |
| `langsmith.api_url` | `https://api.smith.langchain.com` | Explicit hosted or self-hosted HTTP(S) API endpoint |
| `langsmith.api_key` | `null` | Direct private secret; enabled tracing requires this or one environment reference |
| `langsmith.api_key_env` | `null` | Explicit environment variable containing the trace key; e.g. `EVIDENCE_LAB_LANGSMITH_API_KEY` |
| `langsmith.workspace_id` | `null` | LangSmith workspace ID, when required by the selected key |
| `langsmith.timeout_seconds` | `2` | Export wait/transport timeout in seconds; >0, ≤10; a running SDK thread can finish after the caller times out |

### Environment variables and Task/dev controls

| Variable/control | Scope and default | Meaning |
| --- | --- | --- |
| `CONFIG=path` | Task; `configs/mock.yaml` | Selects YAML for native app/dev/backup/restore tasks. It does not select the Compose-mounted file. |
| `EVIDENCE_LAB_CONFIG` | Compose; `./configs/mock.compose.yaml` | Host configuration file mounted read-only at `/app/configs/runtime.yaml`. |
| `EVIDENCE_LAB_API_URL` | Vite development; `http://127.0.0.1:8000` | Proxy target for `/api` and `/health`. `task dev` sets it automatically from the selected runtime host/port, translating wildcard hosts to loopback. It is not a browser-exposed API key or production config variable. |
| `--dashboard-port` | `task dev -- --dashboard-port 5174`; `5173` | Vite dev TCP port, 1–65535; strict port selection fails rather than silently switching ports. |
| A profile's `api_key_env` name | Active live provider only; none | Arbitrary explicitly configured credential variable, e.g. `EVIDENCE_LAB_EMBEDDING_API_KEY`; the example name has no automatic meaning without the YAML reference. |
| `langsmith.api_key_env` name | Enabled tracing in either runtime mode; none | Explicit tracing credential variable, e.g. `EVIDENCE_LAB_LANGSMITH_API_KEY`; forward it to API/worker environments or use a direct private YAML key. |
| `EVIDENCE_LAB_DATABASE_DSN` | Direct Alembic invocation; unset | DSN fallback for Alembic when an application-provided migration connection is absent. Normal `task app:migrate` reads `database.dsn` from YAML. |
| `EVIDENCE_LAB_TEST_DSN` | Integration tests; unset | Dedicated PostgreSQL/pgvector test database. Tests create/drop private schemas; unset skips database-dependent tests. |
| `EVIDENCE_LAB_TEST_NATIVE_ADMIN_DSN` | Native restore tests; unset | Explicit opt-in connection for creating/deleting uniquely named disposable test databases. Requires database-creation permission, pgvector and native `pg_dump`/`pg_restore`; unset skips that gate. |
| `EVIDENCE_LAB_TEST_BACKEND` | Tests; unset | `pglite` selects test-only fixture handling and skips native-only gates. It never enables a production storage fallback. |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | Compose database; `evidence_lab`, `evidence`, `evidence` | Database initialization settings currently fixed in `compose.yaml`; they are not interpolated host environment overrides. The sample credentials are for the loopback mock setup. |

The backup/restore helpers derive libpq variables from the YAML DSN and discard inherited `PG*` variables, so stale shell values cannot redirect them. Supported mappings are `host→PGHOST`, `hostaddr→PGHOSTADDR`, `port→PGPORT`, `dbname→PGDATABASE`, `user→PGUSER`, `password→PGPASSWORD`, `sslmode→PGSSLMODE`, `sslrootcert→PGSSLROOTCERT`, `sslcert→PGSSLCERT`, `sslkey→PGSSLKEY`, `sslcrl→PGSSLCRL`, `sslcrldir→PGSSLCRLDIR`, `connect_timeout→PGCONNECT_TIMEOUT`, `options→PGOPTIONS`, `application_name→PGAPPNAME`, `target_session_attrs→PGTARGETSESSIONATTRS`, `channel_binding→PGCHANNELBINDING`, `service→PGSERVICE`, `passfile→PGPASSFILE` and `gssencmode→PGGSSENCMODE`. These are derived subprocess settings, not independent application YAML overrides.

## What the release gate enforces

The release flow above checks citations, every answer block and `global.task_scope`, `global.internal_consistency`, `global.counterevidence`. Verdicts bind to answer/evidence hashes and round IDs.

Incomplete verification is a technical failure. Public endpoints hide drafts; documents and model output render as text without tools or instruction execution.

Input: English UTF-8 text, Markdown and readable PDFs. OCR is external; ambiguous extraction requires review. Source offsets use normalized Unicode code points.

## Evaluate the system

The bundled dataset is synthetic and unqualified; human quality review remains pending.

```bash
task app:cli -- evaluate --config configs/mock.yaml
task app:cli -- evaluate --config configs/mock.yaml --execute --output artifacts/demo
```

Dry run estimates calls/costs. Execution exports JSON, CSV, Markdown and annotation templates. A/B/C/D share an initial draft and evidence pack:

| Variant | Behavior |
|---|---|
| A | Ungated draft for diagnostic baseline |
| B | Structural checks |
| C | Structural and semantic gate |
| D | Semantic gate plus at most one fully rechecked repair |

CLI datasets use `--dataset PATH --split development|test`; HTTP accepts only the bundled mock demo. Live studies need source/gold data and a positive evaluation budget.

Use `eval-review` for hash-bound human annotations. [Evaluation](docs/evaluation.md) covers metrics, splits and qualification; [experiments](docs/experiments.md) covers independent load/repeatability studies.

Planned held-out study: 140 answerable, 40 missing-evidence and 20 conflict questions, plus 200 supported claims and 200 reviewed mutations. Live results are not supplied.

## Tests, recovery and operations

Run the offline contract suite without inference:

```bash
task test:offline
task lint
```

Full checks require dedicated `EVIDENCE_LAB_TEST_DSN`; native restore also needs `EVIDENCE_LAB_TEST_NATIVE_ADMIN_DSN` and PostgreSQL client tools. Missing prerequisites are skips. Export JUnit with `task app:test -- --junitxml=artifacts/pytest.xml`.

Engineering test results do not qualify model release. Qualification requires current reviewed evaluation, external fault evidence and independent studies.

Assess completed qualification evidence:

```bash
task app:cli -- qualify --config configs/private.yaml artifacts/reviewed-evaluation/report.json \
  --fault-artifact artifacts/externally-collected-fault-study.json \
  --load-artifact artifacts/live-load.json \
  --repeatability-artifact artifacts/repeatability.json \
  --policy-id heldout-policy-v1 --output policies/heldout-policy-v1.json
```

Only a passing artifact enables live gated release with matching `policy_id`/`policy_path`. Code, profile, prompt or runtime changes invalidate it; key rotation does not. Study help: `task app:cli -- experiments -- --help`.

`/health/live` and `/health/ready` make no inference calls. `/api/status` exposes redacted mode, limits, policy and budget state.

Backup and empty-target restore:

```bash
task backup CONFIG=configs/private.yaml -- --output artifacts/backup.dump
task restore CONFIG=configs/private-restore.yaml -- --input artifacts/backup.dump
```

Use matching `pg_dump`/`pg_restore` clients and an empty restore target with pgvector. Back up private YAML separately. Export audit material before deleting sources, which purges dependent evidence/traces.

## Workspace tasks and structure

One root `.venv`/`uv.lock` serves Python; pnpm manages the Vue package and Biome. Task coordinates both apps.

Edit Vue views, components, composables, API/types and styles under `apps/dashboard/src/`; never edit generated `dist/`.

The wheel contains backend/migrations; dashboard assets are separate. Wheel serving requires `apps/dashboard/dist/` and the repository root as working directory.

Run from the repository root:

```bash
task setup         # install locked uv/pnpm dependencies and build the dashboard
task lock          # deliberately update workspace dependency locks
task lint          # Ruff, Basedpyright, dashboard type checking and Biome lint
task format        # format Python with Ruff and frontend sources with Biome
task test          # Python and frontend tests; integration prerequisites apply
task check         # lint, then the complete test suite
task typecheck     # check Python and dashboard TypeScript types
task build         # build dashboard assets, then the Python distribution
task clean         # remove generated development/build caches
```

### Complete command reference

Use `CONFIG` for native tasks and `EVIDENCE_LAB_CONFIG` for Compose. Forward CLI options after `--`:

```bash
task dev CONFIG=configs/mock.yaml -- --dashboard-port 5174
task app:worker CONFIG=configs/private.yaml -- --once
task app:cli CONFIG=configs/private.yaml -- config-check
task app:test -- --junitxml=artifacts/pytest.xml
```

| Task | Behavior |
|---|---|
| `task`, `task --list` | List available tasks |
| `task setup` | Install locked Python/frontend dependencies; build dashboard |
| `task dev` | Prepare default mock DB; supervise API, worker and Vue hot reload |
| `task lock` | Update uv and pnpm dependency locks deliberately |
| `task lint` | Ruff and Basedpyright for backend/tests/tooling, Vue/TypeScript type checking and Biome |
| `task format` | Format Python and frontend/shared tooling sources |
| `task typecheck` | Python Basedpyright and dashboard Vue/TypeScript checks |
| `task test` | Backend and dashboard tests; missing DB prerequisites produce skips |
| `task test:offline` | Backend tests excluding integration/native markers, plus dashboard tests |
| `task check` | Lint then full tests, sequentially |
| `task build` | Dashboard assets and backend wheel/source distribution |
| `task clean` | Remove generated build/test caches; preserve dependencies and runtime data |
| `task app:cli -- COMMAND [ARGS]` | Build dashboard and run an operator command |
| `task app:serve` | Build dashboard and start FastAPI; no worker or DB bootstrap |
| `task app:worker` | Process durable jobs; accepts `--once` and `--concurrency N` |
| `task app:migrate` | Apply migrations and initialize the default corpus |
| `task app:seed` | Ingest bundled demo sources; run jobs locally with `--wait` |
| `task app:lint` | Ruff backend source/test checks |
| `task app:typecheck` | Basedpyright for backend, Python tests and tooling using the root `.venv` |
| `task app:test` | Backend tests, with dashboard build dependency |
| `task app:test:offline` | Backend tests excluding DB integration/native markers |
| `task app:build` | Build dashboard dependency and Python distributions into root `dist/` |
| `task dashboard:dev` | Standalone Vite hot reload; API must already be running |
| `task dashboard:serve` | Build dashboard and run FastAPI on one origin |
| `task dashboard:typecheck` | Check Vue templates and TypeScript |
| `task dashboard:lint` | Biome lint and formatting checks |
| `task dashboard:test` | Vitest dashboard behavior tests |
| `task dashboard:build` | Type-check and compile into `apps/dashboard/dist/` |
| `task compose:db` | Start only Compose PostgreSQL; no migrations |
| `task compose:up` | Build/start Compose DB, migration job, API and worker |
| `task compose:seed` | Seed using the running Compose API container's configuration |
| `task compose:stop` | Stop stack processes, preserving containers and volumes |
| `task compose:down` | Remove stack containers/network, preserving volumes |
| `task backup -- --output PATH` | Native PostgreSQL backup using selected `CONFIG` |
| `task restore -- --input PATH` | Restore to the selected empty PostgreSQL target |

CLI: `evidence-lab` or `.venv/bin/python -m evidence_lab`. Options: `task app:cli -- COMMAND --help`.

| CLI command | Purpose and main arguments |
|---|---|
| `config-check` | Validate selected YAML and print redacted settings |
| `migrate` | Apply PostgreSQL migrations |
| `serve` | Start API/compiled dashboard; optional `--host`, `--port` |
| `worker` | Process jobs; optional `--once`, `--concurrency` |
| `seed` | Enqueue synthetic demo documents; `--wait` processes them locally |
| `ingest PATH` | Upload a local source; `--corpus`, `--document-id`, `--wait` |
| `query QUESTION` | Enqueue question; `--corpus`, `--wait` |
| `retrieve QUESTION` | Preview hybrid evidence; `--corpus` |
| `smoke` | Plan endpoint checks; `--execute` makes budgeted calls |
| `evaluate` | Plan/execute study; `--dataset`, `--split`, `--annotations`, `--output`, `--execute` |
| `eval-export JOB_ID` | Export stored evaluation; `--output` |
| `eval-review REPORT` | Apply human review without inference; required `--annotations`, optional `--output` |
| `qualify REPORT` | Assess reviewed evaluation plus required fault/load/repeatability artifacts and policy ID |
| `experiments` | Delegate study arguments; `experiments -- --help` lists study commands |
| `inspect ID` | Read job; `--run` selects query run and `--trace` includes private operator diagnostics |
| `cleanup` | Apply configured retention; preserve source references needed by retained runs |
| `policy-fingerprint` | Print current semantic release-policy identity |
| `offline-tests` | Run backend offline contract tests; root `task test:offline` also runs dashboard tests |

### Project skills and agent roles

Conventions: [AGENTS.md](AGENTS.md). Skills: `.agents/skills/`. Roles: `.agents/agents/`, linked through `.codex/agents`.

| Skill | Agent role | Scope |
|---|---|---|
| [evidence-lab-development](.agents/skills/evidence-lab-development/SKILL.md) | `evidence_backend` | Python/backend, contracts and workspace changes |
| [evidence-lab-dashboard](.agents/skills/evidence-lab-dashboard/SKILL.md) | `evidence_dashboard` | Vue/TypeScript dashboard |
| [evidence-lab-verification](.agents/skills/evidence-lab-verification/SKILL.md) | `evidence_verifier` | Independent scoped engineering checks |
| [evidence-lab-security-review](.agents/skills/evidence-lab-security-review/SKILL.md) | `evidence_security_reviewer` | Explicitly requested security reviews with concrete findings |

Roles inherit session settings; parallel delegation requires approval. Coordinate shared contracts/build outputs.

## Repository map

| Path | Contents |
|---|---|
| `apps/evidence-lab/src/evidence_lab/config.py` | Strict YAML schema, active-profile validation and redaction |
| `apps/evidence-lab/src/evidence_lab/providers/` | OpenAI-compatible/native adapters, transport and fixture mode |
| `apps/evidence-lab/src/evidence_lab/storage.py`, `apps/evidence-lab/src/evidence_lab/migrations/` | PostgreSQL, vectors, immutable versions, leases and cost reservations |
| `apps/evidence-lab/src/evidence_lab/ingestion.py`, `retrieval.py` | Extraction, stable chunks, embedding cache, exact hybrid retrieval |
| `apps/evidence-lab/src/evidence_lab/engine.py`, `graph.py`, `policy.py` | LangGraph workflow, frozen evidence, release gate and bounded repair |
| `apps/evidence-lab/src/evidence_lab/api.py`, `worker.py` | HTTP API and durable execution |
| `apps/evidence-lab/src/evidence_lab/integrations/`, `memory.py`, `langsmith_trace.py` | Standard model/tool interfaces, conversation context and metadata tracing |
| `apps/dashboard/src/`, `apps/dashboard/index.html`, `apps/dashboard/public/` | Vue/TypeScript dashboard, HTML entrypoint, CSS and favicon |
| `apps/dashboard/dist/` | Generated dashboard build served directly by FastAPI |
| `apps/evidence-lab/src/evidence_lab/evaluation.py`, `experiments.py` | Paired evaluation, independent studies and qualification evidence |
| `apps/evidence-lab/tests/` | Contract, integration and native PostgreSQL verification |
| `tooling/` | Alembic/TypeScript tooling and development, backup, restore and cleanup scripts |
| `data/`, `configs/`, `docs/` | Shared fixtures, operator configuration and documentation |
| `pyproject.toml`, `uv.lock` | Python workspace, shared development tools and dependency lock |
| `package.json`, `pnpm-workspace.yaml`, `pnpm-lock.yaml`, `biome.json` | Frontend workspace, shared tooling and dependency lock |
| `Taskfile.yml`, application Taskfiles | Task orchestration for both stacks |

This PoC has not established production readiness or live model quality.

See [dashboard development](apps/dashboard/README.md) for component conventions and focused frontend commands.
