# Evidence Lab operator and development guide

Use the [project README](../README.md) for a quick start and the full YAML configuration reference. Run commands below from the repository root.

- [Workspace layout](#names-and-workspace-layout)
- [Demo](#start-the-complete-demo) and [local development](#local-development)
- [Architecture and data flow](#architecture-and-data-flow)
- [LangGraph showcase](#langgraph-showcase)
- [Provider configuration](#configure-your-providers) and [Cloudflare Clef](#cloudflare-clef-verifier)
- [Browser keys](#bring-your-own-key-byok) and [MCP](#agent-rag-through-mcp)
- [Live storage](#move-from-mock-to-live-storage)
- [Workspace deletion](#delete-a-workspace)
- [Answer release](#what-the-release-gate-enforces) and [evaluation](#evaluate-the-system)
- [Tests, recovery and operations](#tests-recovery-and-operations)
- [Task and CLI command reference](#workspace-tasks-and-structure)
- [Repository map](#repository-map)

## Names and workspace layout

Names: CLI/distribution `evidence-lab`, Python package `evidence_lab`, frontend `@evidence-lab/dashboard`, Compose project `evidence-lab`, image `evidence-lab:local`. The checkout may remain `rag-poc`. Deployments use `evidence_*` tables and `EVIDENCE_LAB_*` environment variables.

For GitHub Actions deployment to Cloudflare Pages and a GHCR-backed VPS, see
[deployment setup](deployment.md).

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

Compose runs PostgreSQL/pgvector, migrations, API and worker on localhost. Sample DB credentials: `evidence:evidence`. Pinned images are recorded in [container-images.json](container-images.json).

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
  Engine --> Models["integrations/models.py: structured generation"]
  Engine --> VerifyTool["integrations/tools.py: verify_frozen_evidence"]
  Models --> Hub["providers: embedding, generation, verification"]
  VerifyTool --> Hub
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

Fixtures remain unqualified. See [evaluation](evaluation.md), [experiments](experiments.md) and [test commands](#tests-recovery-and-operations).

## LangGraph showcase

The query graph invokes a corpus-scoped retrieval tool, a structured LangChain chat model and a scoped verification tool (native Clef or the configured chat verifier). The dashboard shows stage completion, repair executions and timings. Provider adapters retain exact endpoint contracts, bounded retries and PostgreSQL spending reservations.

OpenAI-compatible calls use `ChatOpenAI` and `OpenAIEmbeddings` through a single-use HTTP guard that reserves spending in PostgreSQL before sending. Native Clef retains its adapter. Prompts, strict schemas and structured-output composition also use LangChain built-ins. See [provider integrations](../apps/evidence-lab/src/evidence_lab/providers/README.md) for the reuse map and custom wire/accounting boundaries.

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

**Clef is a LangChain verification tool backed by the native `cloudflare_clef` adapter.** The graph invokes `verify_frozen_evidence` with typed question/draft arguments; frozen evidence, budget context and round are bound by the application. Your configured chat model still generates answers. See [Cloudflare's Clef reference](https://developers.cloudflare.com/workers-ai/models/clef/).

The tool reuses `ProviderHub.verify` and its PostgreSQL reservations, bounded
retries and strict choice/probability validation. Its `ToolMessage` contains summary
checks and a full `VerificationResult` artifact with answer/evidence hashes. The
graph applies the release policy to that artifact; a tool result alone does not
release an answer. Clef probabilities remain uncalibrated. See the
[tool example](../apps/evidence-lab/src/evidence_lab/providers/README.md#clef-in-the-langchain-toolset).

```mermaid
flowchart LR
  Graph[LangGraph verification stage] --> Tool[verify_frozen_evidence]
  Scope[Frozen evidence + ledger context + round] --> Tool
  Tool --> Hub[ProviderHub.verify]
  Hub --> Clef[Budgeted native Clef request]
  Clef --> Artifact[Validated checks + typed artifact]
  Artifact --> Gate[Existing release policy]
```

### Activate Clef

1. Copy the complete [clef.example.yaml](../configs/clef.example.yaml) to `configs/private.yaml` for OpenAI embeddings/generation plus Clef. See [configuration setup](../configs/README.md) for the exact substitutions. For other providers, start from `configs/live.example.yaml`.
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

Use a new corpus when switching from fixture embeddings. In `shadow` mode, checked candidates appear only in the operator trace. Set `verification.mode: verified` to automatically publish answers after every check passes, without claiming policy qualification. Use `verification.mode: gated` with a matching qualification artifact for evaluated release. Restart API/worker after changing verifier settings.

## Bring your own key (BYOK)

Open **Workspace connection** using the runtime badge or sidebar connection button.
Select **Fixture mode** for deterministic demos or **Live mode** for your providers.
Enter two keys: **LLM provider key** (shared by embeddings and chat) and
**Cloudflare key** (Clef verifier). Save or remove each key in the browser.

In **Live endpoint setup**, choose **Workspace embeddings** to reuse existing
compatible PostgreSQL/pgvector vectors and the server-configured query embedding
setup. This keeps demo fixture embeddings deterministic while chat and Clef use
live endpoints. Alternatively choose **Custom embedding endpoint** and enter its
complete URL, model, dimensions, limits and price; changing that profile requires
a new corpus and re-upload. Enter your chat endpoint, model and Cloudflare account ID. Confirm
embedding dimensions, token limits, chat output format and all three provider
prices. Model dimensions, token limits, output-limit parameter and provider prices
start unset because they depend on your selected models. Plain JSON text is the
initial output format; structured formats require model support. Fields marked
`*` are required for live mode; the operator token remains optional unless access
protection is enabled. Set your spending budget; zero prevents live calls. Save setup, then select
live mode. Saving keys, saving setup and changing modes make no model calls.
Select fixture mode to edit or remove saved setup. Switching modes clears the
current answer and conversation; custom embedding profile changes require a new corpus
and re-uploaded sources. Workspace embeddings preserve the base embedding profile. Live mode starts in shadow verification and does not
release unqualified answers.

See the [BYOK guide](byok.md) for required fields, embedding compatibility,
credential handling, operator sessions and recovery.

Keys, setup and mode are saved only in this browser, scoped to the server's base
configuration. No YAML edits or server restart are needed for dashboard BYOK.
Use **Sign in** in the dashboard header for an operator session. The separate
sign-in dialog accepts the server's operator token. While signed in, mode,
providers, keys, models, limits and budget come from the server; browser overrides
are omitted and their form is hidden. The token stays in page memory. **Sign out**
clears it and restores saved browser setup, if workspace access permits it.
Existing private YAML server-key configurations continue to work for CLI workers.

Keys stay in local storage for this browser and configuration. They are sent in
an inference request header and used by a request-scoped API task, never written
to YAML, PostgreSQL jobs/results, logs or traces. The existing provider adapters,
budget ledger and verification gate remain in use. No inference is triggered by
saving a key. Use HTTPS when accessing the application remotely; browser storage
is accessible to scripts running on the application’s origin.

BYOK ingestion, queries and evaluations execute in the API process. Ordinary
workers skip these jobs. An API restart interrupts that work: re-upload the same
document to resume its queued/expired job, retry a stopped ingestion/evaluation,
or cancel and submit a new query. Background CLI operations and unattended
recovery require the existing server-key mode. No live endpoint performance or
model quality is implied by the deterministic tests.

## Agent RAG through MCP

Connect a Streamable HTTP client to `http://127.0.0.1:8000/api/mcp/`, or the HTTPS
backend equivalent for remote agents. Every request requires the configured
operator bearer token, even in fixture mode; without it MCP returns 401.

| Tool | Purpose |
|---|---|
| `search_evidence(question, corpus_id="default")` | Retrieve bounded hybrid-search evidence with immutable citations and fixture labels |
| `get_evidence_source(corpus_id, version_id, evidence_id)` | Read a cited excerpt while checking corpus membership and source deletion |

Results have `verified_answer: false`. MCP does not generate or verify answers,
accept BYOK overrides, or expose mutation tools. Search can make a budgeted query
embedding call using the server configuration. Source reads make no provider calls.
Live MCP search requires `runtime.credentials: server`; browser BYOK configurations
cannot supply keys to it.
The global corpus allowlist is not user isolation, and the shared operator token
also grants administrative REST access. Give it only to trusted operators/agents.

See [MCP setup and client example](agent-rag-interface.md) for remote Host/Origin
configuration, payload bounds, safe errors and the deferred OAuth integration.

## Move from mock to live storage

After changing the embedding space, create a new corpus and re-upload sources. Existing vectors cannot be mixed with the new space. Compose database hostname: `db`.

To run a private configuration in Compose:

```bash
EVIDENCE_LAB_CONFIG=./configs/private.yaml task compose:up
```

Restart API/worker after configuration changes. For a separate project, free the ports and use `docker compose -p evidence-lab-live ...`.

Compose database credentials and private DSNs must match.

Containers run as UID `10001`; mounted YAML must be readable. Mount `api_key_file` read-only or explicitly forward the selected `api_key_env` variable.

## Configuration reference

See the complete [configuration reference](../README.md#configuration-reference) in the project README.

## Delete a workspace

On the Documents screen, choose **Delete workspace** and type its name to confirm.
This permanently removes the corpus and its documents, source bytes and versions,
chunks, vectors, conversations, runs, events and related jobs. Shared embedding
cache entries remain only while another corpus uses them. Anonymous spend and
concurrency accounting remain so deletion cannot reset configured budgets;
previously exported LangSmith traces and backups require separate deletion.
The dashboard selects another workspace or asks you to create one when none remain.
Server startup recreates an empty `default` workspace if it was deleted.

The authenticated API is `DELETE /api/corpora/{corpus_id}`. Wait for unscoped
evaluations to finish or cancel them before deleting a workspace, since their
pending corpus dependencies cannot be determined safely.

## What the release gate enforces

See [qualification and answer-release assurance](qualification-assurance.md)
for the mechanisms, qualification evidence, runtime artifact checks, and trust limits.

The release flow above checks citations, every answer block and `global.task_scope`, `global.internal_consistency`, `global.counterevidence`. Verdicts bind to answer/evidence hashes and round IDs.

For automatic release after these checks, configure:

```yaml
verification:
  mode: verified
```

This mode retains the configured score threshold, repair limit and all failure
guards. Live answers are labeled as checked under an unqualified policy. It
applies to new queries; historical failed or shadow runs are unchanged.

Incomplete verification is a technical failure. Public endpoints hide drafts; documents and model output render as text without tools or instruction execution.

Signed-in operators can use **Release with operator approval** on a checked shadow run and enter
a reason. The server rechecks the stored draft, verification bindings and active
sources before publishing it as **Operator-approved**. This exception applies to
that run only; it does not qualify the evaluation policy or change future runs.
Use **Revoke operator release** to hide an operator-approved answer again. Both actions
are audited, make no model calls, and require the configured operator token.
Revocation also removes that answer from copied conversation context.

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

Use `eval-review` for hash-bound human annotations. [Evaluation](evaluation.md) covers metrics, splits and qualification; [experiments](experiments.md) covers independent load/repeatability studies.

Planned held-out study: 140 answerable, 40 missing-evidence and 20 conflict questions, plus 200 supported claims and 200 reviewed mutations. Live results are not supplied.

## Tests, recovery and operations

Run the offline contract suite without inference:

```bash
task test:offline
task lint
```

Full checks require dedicated `EVIDENCE_LAB_TEST_DSN`; native restore also needs `EVIDENCE_LAB_TEST_NATIVE_ADMIN_DSN` and PostgreSQL client tools. Missing prerequisites are skips. Export JUnit with `task app:test -- --junitxml=artifacts/pytest.xml`.

[GitHub Actions CI](../.github/workflows/ci.yml) runs `task setup`, `task check` and `task build` on pull requests, pushes to `main` and manual runs. It uses a disposable PostgreSQL 17/pgvector service and matching native clients, rejects skipped Python tests, and retains JUnit results and build artifacts for seven days. No model credentials are needed.

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
task ci            # same checks/builds as GitHub CI; requires dedicated test DSNs
task typecheck     # check Python and dashboard TypeScript types
task build         # build dashboard assets, then the Python distribution
task clean         # remove generated development/build caches
```

`task ci` runs the same sequence as GitHub CI and writes `artifacts/pytest.xml`.
It requires PostgreSQL with pgvector, `pg_dump`/`pg_restore`, and explicit dedicated
test DSNs. The admin DSN authorizes the native restore tests to create and remove
their own temporary databases. Never use an application database:

```bash
EVIDENCE_LAB_TEST_DSN=postgresql://evidence:evidence@127.0.0.1:5432/evidence_test \
EVIDENCE_LAB_TEST_NATIVE_ADMIN_DSN=postgresql://evidence:evidence@127.0.0.1:5432/evidence_test \
  task ci
```

Missing prerequisites or skipped backend tests fail this gate. Use `task test:offline`
for checks without native PostgreSQL. Local `task ci` does not publish or deploy.
See [CI checks and release flow](ci.md) and [deployment provisioning](deployment.md).

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
| `task ci` | Locked setup, dependency checks, lint/native tests, deployment tests, no-skip gate and builds |
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

Conventions: [AGENTS.md](../AGENTS.md). Skills: `.agents/skills/`. Roles: `.agents/agents/`, linked through `.codex/agents`.

| Skill | Agent role | Scope |
|---|---|---|
| [evidence-lab-development](../.agents/skills/evidence-lab-development/SKILL.md) | `evidence_backend` | Python/backend, contracts and workspace changes |
| [evidence-lab-dashboard](../.agents/skills/evidence-lab-dashboard/SKILL.md) | `evidence_dashboard` | Vue/TypeScript dashboard |
| [evidence-lab-verification](../.agents/skills/evidence-lab-verification/SKILL.md) | `evidence_verifier` | Independent scoped engineering checks |
| [evidence-lab-security-review](../.agents/skills/evidence-lab-security-review/SKILL.md) | `evidence_security_reviewer` | Explicitly requested security reviews with concrete findings |

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
| `apps/evidence-lab/src/evidence_lab/mcp_server.py` | Authenticated read-only MCP evidence tools and transport limits |
| `apps/evidence-lab/src/evidence_lab/runtime_settings.py`, `byok.py` | Validated browser mode/provider overrides and request-scoped BYOK credentials |
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
| `.github/workflows/`, `deploy/`, `apps/dashboard/functions/` | CI, Pages deployment/proxy and digest-pinned VPS deployment |

This PoC has not established production readiness or live model quality.

See [dashboard development](../apps/dashboard/README.md) for component conventions and focused frontend commands.
