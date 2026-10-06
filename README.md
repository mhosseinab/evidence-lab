# Evidence Lab

Document-grounded question answering with a Vue dashboard, LangGraph workflows, PostgreSQL/pgvector storage and optional LangSmith tracing. Answers pass citation and verification checks before release.

The default demo uses deterministic fixtures and makes no inference requests. Models and tokenizers are never downloaded or hosted. Files are shared by everyone with workspace access; there is no per-user document isolation.

## Start the complete demo

Install Task and Docker with Compose, then run from the repository root:

```bash
task compose:up
task compose:seed
```

Open **http://127.0.0.1:8000**. Upload a text file, ask a question and inspect its citation. For a reproducible example, use the [demo walkthrough](docs/operator-guide.md#start-the-complete-demo).

Stop services with `task compose:stop`; database data is preserved.

## Local development

Requirements: Python 3.12+, uv, Task, Node.js 24+, pnpm and Docker Compose.

```bash
task setup
task dev
```

Open **http://127.0.0.1:5173/static/**. The default mock configuration prepares PostgreSQL and migrations. Ctrl+C stops the API, worker and Vite; the database stays running. Restart after Python changes.

For custom configuration and compiled dashboard serving, see [local development](docs/operator-guide.md#local-development).

## Common tasks

| Task | Command or guide |
| --- | --- |
| Check the workspace | `task check` |
| Reproduce GitHub CI locally | `task ci` — [prerequisites and checks](docs/ci.md#reproduce-locally) |
| Build both applications | `task build` |
| Configure live providers | [Provider setup](docs/operator-guide.md#configure-your-providers) and [sample configurations](configs/README.md) |
| Use Cloudflare Clef | [Activation steps](docs/operator-guide.md#cloudflare-clef-verifier) |
| Deploy the dashboard and API | [Deployment guide](docs/deployment.md) |
| Delete a workspace | [Dashboard control and purge scope](docs/operator-guide.md#delete-a-workspace) |

To automatically publish answers that pass all verification checks:

```yaml
verification:
  mode: verified
```

This applies to new queries and retains the verification threshold, repair limit and failure guards. It does not qualify the model policy. See [answer release and qualification](docs/qualification-assurance.md) for release modes, operator overrides and assurance limits.

## Read next

| Guide | What it explains |
| --- | --- |
| [Operator and development guide](docs/operator-guide.md) | Architecture, workflows, complete commands, testing and recovery |
| [Evaluation](docs/evaluation.md) | Dataset preparation, human review, metrics and qualification procedure |
| [Qualification assurance](docs/qualification-assurance.md) | Per-answer checks, policy gates, artifact validation and trust limits |
| [Experiments](docs/experiments.md) | Live load and verifier repeatability measurements |
| [Browser keys](docs/byok.md) | BYOK, operator sessions and credential handling |
| [Agent RAG](docs/agent-rag-interface.md) | Read-only MCP tools and client setup |
| [Contracts](docs/contracts.md) | API, provider and storage boundaries |
| [Documentation index](docs/README.md) | All project guides |

Fixture runs prove software plumbing. Human-reviewed held-out model quality and live performance remain unmeasured until the required studies are actually run.

## Configuration reference

Every supported YAML field, default and allowed value is documented below. For setup examples, see [configs](configs/README.md).

<details>
<summary>Expand the complete configuration reference</summary>


Schema: [config.py](apps/evidence-lab/src/evidence_lab/config.py). Select YAML with Task `CONFIG=...` or CLI `--config`. Samples: `mock.yaml` (local), `mock.compose.yaml` (DB host `db`), `live.example.yaml` (incomplete live template), `clef.example.yaml` (complete OpenAI/Clef template requiring credentials). Tables show schema defaults; samples may override them.

Strict UTF-8 YAML, ≤1 MiB. Unknown/duplicate keys, wrong types and non-finite values fail validation. Required fields have no default; `null` means unset.

The tables below cover every application YAML field. Enum values are listed
explicitly; numeric fields accept values within their documented bounds. Boolean
fields accept YAML `true` and `false`: `true` enables the described behavior,
`false` disables it. Strings and mappings use the stated format rather than a
fixed list of values. Only fields described as optional accept `null`; omitting a
field uses its default. Credential environment references are explicit
`api_key_env` settings, not `${...}` interpolation throughout the YAML.

| Root field | Default | Meaning |
| --- | --- | --- |
| `config_version` | `1` | Only schema version `1` is supported. |
| `runtime`, `agent_rag`, `database`, `ingestion`, `retrieval`, `verification`, `budgets`, `evaluation`, `memory`, `langsmith` | Section defaults below | Optional sections use their schema defaults. |
| `profiles` | Required | Nonempty mapping of named provider contracts. Names start with a letter or number and contain up to 96 letters, numbers, `.`, `_` or `-`. |
| `roles` | Required | Assigns pipeline roles to profiles. |

### Runtime and database

| YAML field | Default | Meaning and constraints |
| --- | --- | --- |
| `runtime.mode` | `mock` | `mock` uses deterministic fixtures without remote inference or model credential resolution; `live` uses configured remote providers. Explicit LangSmith tracing is configured separately. |
| `runtime.embedding_mode` | `null` | Embedding execution override: `mock` or `live`. Mock runtime always uses fixture embeddings; live runtime defaults to live embeddings. Existing fixture vectors remain visibly labeled when used with live generation. |
| `runtime.credentials` | `server` | `server` resolves private provider credentials; live `browser` profiles must omit server credential fields and receive request-scoped BYOK keys. |
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
| `runtime.operator_token` | `null` | Nonempty shared operator secret. When configured, all `/api/` requests require `Authorization: Bearer <token>`. Server-funded API inference and MCP require it even when general access is otherwise open. Health and dashboard assets remain accessible. Use dashboard **Sign in**; blank tokens fail validation. |
| `database.dsn` | `postgresql://evidence:evidence@localhost:5432/evidence_lab` | PostgreSQL URL with a host and database name; accepts `postgresql` or `postgres`. Credentials are redacted from public configuration. No production storage fallback. |

### Agent RAG

| YAML field | Default | Meaning and constraints |
|---|---|---|
| `agent_rag.allowed_corpora` | `[default]` | Global MCP corpus allowlist; 1–100 valid corpus IDs. Does not limit ordinary operator REST access. |
| `agent_rag.allowed_hosts` | Loopback names/IPs with bare and wildcard-port entries | SDK Host allowlist. Add the actual public backend hostname for remote access; see the [exact defaults](apps/evidence-lab/src/evidence_lab/config.py). |
| `agent_rag.allowed_origins` | `["http://localhost:*", "http://127.0.0.1:*"]` | SDK Origin allowlist for authenticated MCP requests. REST writes retain same-origin protection; browser CORS support is not added. |
| `agent_rag.max_request_bytes` | `65536` | MCP request body cap; 1024–1048576 bytes. |
| `agent_rag.max_result_bytes` | `65536` | Structured tool-result cap; 1024–1048576 bytes. The textual copy and protocol overhead also consume wire bytes. |

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
| `verification.mode` | `shadow` | `shadow`, `evaluation`, `verified` or `gated`; `verified` auto-releases checked answers without policy qualification. |
| `verification.policy_id` | `null` | Identifier of the selected qualification policy. |
| `verification.policy_path` | `null` | Path to its qualification artifact. Setting an ID/path alone does not qualify a live gate. |
| `verification.score_threshold` | `null` | Optional scalar verifier threshold; 0–1. |
| `verification.max_answer_blocks` | `8` | Answer block limit; 1–8. |
| `verification.max_answer_bytes` | `8000` | Answer size limit in bytes; 512–64000. |
| `verification.max_repair_bytes` | `16384` | Repair payload size limit in bytes; 512–128000. |
| `verification.max_content_repairs` | `1` | Content repair count; 0 or 1. |
| `verification.evidence_policy` | `frozen` | Only frozen evidence is supported; repair uses the original evidence pack. |

Every verification mode still runs structural and semantic checks before deciding
whether to publish. These are the complete supported mode values:

| `verification.mode` value | Publication behavior |
| --- | --- |
| `shadow` | Keep checked candidates in operator diagnostics. A signed-in operator may approve an individual passing run. Default for live setup. |
| `evaluation` | Keep interactive candidates diagnostic-only, like `shadow`; this value does not automatically start an evaluation study. Use evaluation jobs or CLI commands to run a study. |
| `verified` | Automatically publish each answer after all required checks pass. Does not require a qualification artifact or claim evaluated model quality. Failed checks and technical errors still block release. |
| `gated` | In live mode, require a matching qualified policy artifact before inference and release. In mock mode, passing deterministic fixture answers may publish, explicitly labeled as fixtures. |

`verification.max_content_repairs: 0` disables content repair; `1` allows one
repair after a complete semantic rejection, followed by all checks again. It does
not repair provider/schema failures. `score_threshold: null` uses categorical
verdicts; a number from `0` to `1` additionally requires every check to report a
score meeting that threshold. `evidence_policy: frozen` is the only supported
value: repairs cannot replace the retrieved evidence.

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

The complete protocol and chat capability choices are:

| Field and value | Behavior |
| --- | --- |
| `protocol: embeddings` | OpenAI-compatible embedding requests and vector responses; used by the embeddings role. PostgreSQL/pgvector stores and searches those vectors. |
| `protocol: chat_completions` | OpenAI-compatible chat requests; used for generation, repair or JSON-based verification. |
| `protocol: cloudflare_clef` | Native Cloudflare Clef verification requests and verdicts, not a chat-completions adapter. Requires `runtime.require_openai_compatible: false`; model is `clef` or `clef-flash`. |
| `capabilities.structured_output: json_schema` | Send the strict answer/verification JSON schema through the provider's structured-response format. |
| `capabilities.structured_output: json_object` | Request a JSON object; validate the returned object against the application schema. |
| `capabilities.structured_output: text_json` | Request JSON through the prompt without a structured-response format parameter; still enforce the same application schema. |
| `capabilities.output_limit_parameter: max_completion_tokens` | Send the configured output ceiling using the `max_completion_tokens` request field. |
| `capabilities.output_limit_parameter: max_tokens` | Send the configured output ceiling using the `max_tokens` request field. |

These capabilities must match the actual endpoint. Selecting `json_schema` does
not make an unsupported endpoint implement it. `capabilities.temperature: false`
omits that request parameter; `true` sends zero temperature. `request_dimensions:
false` omits the embeddings dimension parameter; `true` requests the configured
dimension count. `token_counting: conservative_utf8_bytes` is the only counting
method, and `retrieval.search_mode: exact` is the only search mode.

Active live profiles require exactly one credential source and a nonempty printable ASCII key. Mock mode ignores model credential references; explicitly enabled LangSmith resolves its selected tracing key. `api_key_file` resolves relative to the YAML file; dataset, policy and output paths resolve from the working directory.

### Conversation memory and LangSmith

| YAML field | Default | Meaning and constraints |
|---|---|---|
| `memory.enabled` | `true` | Include previously released turns in the conversation's acceptance-time snapshot |
| `memory.max_turns` | `6` | Retained complete question/answer pairs; 1–20 |
| `memory.max_context_bytes` | `8000` | Serialized dialogue byte cap; 256–32000; bundled samples choose 4000 |
| `langsmith.enabled` | `false` | Explicitly enable workflow metadata export |
| `langsmith.capture_content` | `false` | Opt in to redacted provider request/response bodies, including rejected responses; up to 1 MiB per call context. May export question, answer and document text. |
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

</details>
