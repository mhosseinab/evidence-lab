# Evidence Lab

An end-to-end, document-grounded question-answering application with a bounded verification gate. It accepts documents, stores immutable sources, retrieves evidence, creates a cited draft, verifies that exact draft and either releases it, repairs it once, or abstains. The application and PostgreSQL run on your infrastructure. Real inference uses your configured external endpoints.

**No model server, GPU, model weights or tokenizer downloads are included.** The default demo uses deterministic fixtures and makes no inference requests. Its answers are labeled `fixture_only`; they demonstrate application behavior, not model accuracy.

The approved design is in [docs/implementation-plan.md](docs/implementation-plan.md). Actual verification results and remaining deployment checks are in [docs/verification-report.md](docs/verification-report.md).

## Start the complete demo

Requirements: Task and Docker with Compose. Run these commands from the repository root:

```bash
task compose:up
task compose:seed
```

Open **http://127.0.0.1:8000**. The Documents view shows each uploaded source and processing state. The Ask view displays the released answer, immutable citations and retrieved evidence. The Evaluations view runs the bundled synthetic A/B/C/D comparison and exports its raw result.

The Compose stack contains PostgreSQL/pgvector, a migration job, an API and a worker. The PostgreSQL and HTTP ports bind to localhost. The demo database credentials are `rag:rag`; they are local sample values. Images are pinned by digest in `apps/evidence-lab/Dockerfile` and `compose.yaml`, with their registry metadata in `docs/container-images.json`.

For a small, explicit walkthrough, upload a text file containing:

```text
The Atlas refund period is 30 days after purchase.
Refund requests require the order identifier.
```

Ask `What is the Atlas refund period in days?`. Open the citation to inspect the exact source version. In mock mode, adding `[fixture:unsupported]` to the question deliberately inserts an unsupported initial claim; the worker rejects it, repairs once and verifies the entire replacement. Inspect the separately labeled operator trace to see both rounds. `[fixture:conflict]` exercises persistent rejection and abstention. These markers are fixture controls, not features of live models.

Stop processes with `task compose:stop`. This preserves the database volume. The source, vectors, job records, traces and evaluation reports are stored in PostgreSQL.

## Local development

Install Python 3.12 or newer, [uv](https://docs.astral.sh/uv/), [Task](https://taskfile.dev/), Node.js 24 or newer and [pnpm](https://pnpm.io/), and use a PostgreSQL server with pgvector installed. The local configuration defaults to `postgresql://rag:rag@localhost:5432/rag`. You can run just the database from Compose:

```bash
task setup
task compose:db
task app:migrate
task app:seed
```

Start the API and worker in separate terminals:

```bash
task dashboard:serve
task app:worker
```

The worker processes up to four jobs concurrently by default. A persistent reservation ledger enforces the configured outbound concurrency across worker processes. PostgreSQL leases fence every worker publication, and a restart cannot reset remote attempt or cost accounting. The `--once` worker option processes at most one job for debugging.

## Configure your providers

Copy `configs/live.example.yaml` to `configs/private.yaml`. Supply complete values for the three active roles: `embeddings`, `generator` and `verifier`. The example intentionally fails validation until its placeholders are replaced.

```bash
cp configs/live.example.yaml configs/private.yaml
task app:cli -- config-check --config configs/private.yaml
```

**Endpoints and API keys come from this YAML file.** Direct `api_key` values are supported. A profile can instead explicitly select one `api_key_env` or `api_key_file` reference; these are optional. Only the private file contains actual keys. Configuration output, run provenance and errors redact credentials and the database DSN.

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

Providers using one of the supported wire protocols can be changed by configuration. A different protocol needs a separate adapter. There is no automatic provider fallback. `max_tokens` versus `max_completion_tokens`, schema support and embedding dimensions are explicit capabilities.

Keep live `verification.mode: shadow` during setup. A candidate draft is available only in the unverified operator trace; the normal answer field stays empty. Live gated release requires a matching qualification artifact. The same profile can be used for generation and verification, but that does not establish independence; compare an independently selected verifier during evaluation where appropriate.

All live calls require known prices and **positive total and phase budgets**. The phase names are `ingestion`, `queries`, `smoke` and `evaluation`. Zero disables spending in that scope. A nonzero global cap does not enable a phase whose cap is still zero. Every attempt reserves its maximum estimated cost before transport, including retries and repair. Missing usage remains conservatively reserved. Provider billing controls remain separate from this local ledger.

Run a minimal contract smoke check only after configuring the desired endpoints and smoke budget:

```bash
task app:cli -- smoke --config configs/private.yaml
task app:cli -- smoke --config configs/private.yaml --execute
```

The first command is a dry run. The second sends one embedding request, one draft request and one batch verification request, subject to retries and budgets. A smoke pass establishes basic endpoint compatibility, not domain quality.

### Optional Cloudflare Clef

Clef is an explicit native adapter. Set `runtime.require_openai_compatible: false`, complete the inactive `clef_full` profile, and select it as `roles.verifier`. Its full operation URL ends in `@cf/cloudflare/clef`; the native body model is `clef`. The adapter implements the documented state/question/choice contract and validates complete question coverage and probability distributions. It does not route Clef through Chat Completions.

Before a paid comparison, recheck access, the current model contract and prices against the provider. Select any native probability threshold on development data and freeze it before held-out testing. These native values are not calibrated guarantees. FactCG and MiniCheck remain deferred, as requested; neither is downloaded or hosted.

### Move from mock to live storage

Mock and live embeddings have different space identities. Changing endpoint/model/space/dimensions cannot silently mix vectors with an existing corpus. Create a new corpus with the workspace selector and re-upload your original sources under the new embedding configuration. Older corpora remain available for source inspection; queries against a mismatched space fail explicitly before inference. You can also use a separate database or Compose project, preserving the old volume. Update the private YAML DSN to the chosen database. Inside the supplied Compose network the DB hostname is `db`.

To run a private configuration in Compose:

```bash
RAG_CONFIG=./configs/private.yaml task compose:up
```

For a separate live Compose project, stop the demo processes to free the local ports and use `docker compose -p evidence-lab-live ...`. Keep endpoint and credential changes in the mounted private YAML. Restart the API and worker after configuration changes. Optional `runtime.operator_token` protects all `/api/` operations; the UI has an operator-token entry dialog.

Compose defaults to project `evidence-lab` and application image `evidence-lab:local`. An existing `grounded-rag-poc` deployment and its volume are preserved; renaming the source does not move stored data. Stop its processes before starting another stack on the same localhost ports. To deliberately retain that deployment's Compose project and named volume, run the new configuration with the original project name:

```bash
docker compose -p grounded-rag-poc stop
docker compose -p grounded-rag-poc up --build -d
```

Keep database credentials, configuration DSNs, `rag_*` table names and the existing `RAG_CONFIG`/`RAG_TEST_*` environment names unchanged unless separately migrating those persisted contracts. There is no automatic data migration between Compose projects.

The container runs as UID `10001`; the mounted YAML must be readable by that user. Direct `api_key` values work with the supplied mount. If you select `api_key_file`, add a read-only mount for that file at its configured container path. If you select `api_key_env`, explicitly forward the named variable to the API and worker in Compose.

## What the release gate enforces

1. Retrieve exact dense and lexical candidates from the same active-version snapshot. Fuse their ranks with RRF and remove overlapping duplicates.
2. Pack whole evidence chunks within the shared generator/repair/verifier context allowance. The conservative UTF-8-byte bound does not require a tokenizer download.
3. Decode a bounded structured draft containing answer blocks and source IDs. Validate citation membership and verbatim quotations.
4. Verify every block plus `global.task_scope`, `global.internal_consistency` and `global.counterevidence` in one batch. Bind the normalized verdict to answer hash, evidence hash and round ID in application code.
5. On a completed semantic rejection, optionally generate one complete repair against the same frozen evidence. Recheck all blocks and global checks, including unchanged text.
6. Release the exact checked text only if all checks and the qualified policy pass within the deadline. Otherwise return an explicit abstention, shadow result or technical failure.

An incomplete verifier response, timeout or budget exhaustion is a technical failure, not evidence that the question is unanswerable. Public run endpoints never expose intermediate drafts. The operator trace is a separate, explicitly unverified view. Documents and model output are rendered as text, and the adapters do not provide model tools or execute document instructions.

The current ingestion scope is English UTF-8 text, Markdown and readable PDFs, with defaults of 20 MiB, 200 PDF pages, 100 documents and 10,000 active chunks. Scanned pages require OCR outside this implementation. Mixed or ambiguous PDF extraction is marked `needs_review` rather than partially activated. Inspect it and supply a reviewed text version. Exact source offsets use normalized-page Unicode code points; they are not UTF-8 bytes or JavaScript UTF-16 indices.

## Evaluate the system

The bundled fixture dataset contains synthetic examples with deliberately controlled behavior. Its metrics stay **unqualified**, and natural answer-quality fields remain pending human review. Inline fixture evidence does not measure retrieval quality. The ordinary seeded documents separately exercise the retrieval pipeline.

```bash
task app:cli -- evaluate --config configs/mock.yaml
task app:cli -- evaluate --config configs/mock.yaml --execute --output artifacts/demo
```

The first command estimates logical calls and cost bounds. Execution creates a durable evaluation job, preserves every case, and exports JSON, per-question and controlled-case CSVs, a Markdown report and a human annotation template. A/B/C/D share an initial draft and evidence pack:

| Variant | Behavior |
|---|---|
| A | Ungated draft for diagnostic baseline |
| B | Structural checks |
| C | Structural and semantic gate |
| D | Semantic gate plus at most one fully rechecked repair |

Use `--dataset PATH --split development` or `--split test` for an operator-selected domain dataset. Arbitrary filesystem paths are accepted only by the CLI. The HTTP evaluation action accepts the bundled mock demo. Live studies require actual source/gold data, selected profiles and a positive evaluation budget.

The evaluation code reports fixed denominators, missing reviews, operational failures, Wilson intervals, source-family paired comparisons, repair outcomes and usage accounting. Apply independent, hash-bound human answer annotations with `eval-review`. Follow [docs/evaluation.md](docs/evaluation.md) for dataset creation, source-family splits, the gold-label rubric, mutation review and qualification. [docs/experiments.md](docs/experiments.md) covers predeclared repeatability and four-concurrent-request load studies. Paired replay time is not used as a live latency claim.

The target experiment remains the approved 140 answerable, 40 missing-evidence and 20 conflict held-out questions, with 200 supported controlled claims and 200 reviewed mutations. No artificial human labels or successful live benchmark results are supplied.

## Tests, recovery and operations

Run the offline contract suite without inference:

```bash
task test:offline
task lint
```

Engineering verification uses `task check` and `task test`. Database integration tests require an explicitly selected disposable PostgreSQL target in `RAG_TEST_DSN`; native concurrency and restore tests require `RAG_TEST_NATIVE_ADMIN_DSN` and matching PostgreSQL client tools. These tests create isolated test schemas or disposable databases under that authorization. Missing prerequisites remain skips, so retain pytest results and, when needed, produce a JUnit report with `.venv/bin/pytest apps/evidence-lab/tests --junitxml=artifacts/pytest.xml`. A passing offline suite does not establish native recovery or model quality.

Git versioning records source history; no generated source manifest or dedicated fault-artifact runner is included. Qualification still requires an externally collected, complete fault-study artifact matching the reviewed evaluation and current implementation. pytest/JUnit results support engineering checks but are not directly a qualification artifact. Historical studies from prior code are retained as historical evidence and cannot qualify the current implementation.

After the actual held-out review, externally collected fault study and independent experiments are complete, assess them together:

```bash
task app:cli -- qualify --config configs/private.yaml artifacts/reviewed-evaluation/report.json \
  --fault-artifact artifacts/externally-collected-fault-study.json \
  --load-artifact artifacts/live-load.json \
  --repeatability-artifact artifacts/repeatability.json \
  --policy-id heldout-policy-v1 --output policies/heldout-policy-v1.json
```

Use the actual reviewed evaluation, externally collected fault-study and experiment artifact paths. A failed qualification writes the unmet requirements and exits nonzero. Only a passing artifact can enable live `verification.mode: gated` with matching `policy_id` and `policy_path`. Changing code, verification profiles, prompts, packing rules or measured runtime limits invalidates the match. Rotating a key does not. `task app:cli -- experiments -- --help` shows the repeatability/load commands; each execution command defaults to a dry run and requires `--execute` to submit work.

The tests cover malformed and reordered embeddings, duplicate/foreign verdicts, native Clef protocol opt-in, timeouts/retries/cancellation, exact evidence/answer/round binding, one repair, private traces, source snapshots, version activation, quotas and durable ledgers. Integration tests require an explicitly selected disposable PostgreSQL test environment. Native multi-session concurrency and pg_dump/restore are separate required checks; they are not inferred from mock tests.

Health endpoints `/health/live` and `/health/ready` perform no inference. `/api/status` reports selected profile names, mode, policy state, limits and budget accounting without keys. Stage timing includes queue time; the query deadline is measured from acceptance. Cleanup uses configured retention and preserves immutable sources referenced by retained runs.

Native backup and empty-target restore helpers are included:

```bash
task backup CONFIG=configs/private.yaml -- --output artifacts/backup.dump
task restore CONFIG=configs/private-restore.yaml -- --input artifacts/backup.dump
```

Install matching PostgreSQL `pg_dump`/`pg_restore` clients on the operator host. The restore configuration must point to a new empty database with pgvector available. The helper refuses existing user objects and never runs `--clean`; it does not invoke models or re-embed sources. Back up private configuration separately from the database. A source deletion purges affected retained evidence/traces and cancels dependent jobs, so export required audit material before deliberately deleting a source.

`task app:cli -- --help` lists the eighteen operator commands for migration, upload, query, retrieval, worker, smoke, evaluation, trace and retention. Task commands default to `configs/mock.yaml`; select another file with `CONFIG=configs/private.yaml`. Forward detailed CLI options after Task’s `--`, as in `task app:cli CONFIG=configs/private.yaml -- smoke --execute`. The application also accepts `--config PATH` before or after its subcommand.

## Workspace tasks and structure

The workspace contains two runnable applications: `apps/evidence-lab/` holds the Python API and worker, while `apps/dashboard/` holds the TypeScript dashboard. Root `pyproject.toml` defines a virtual uv workspace with the Python app; one `uv.lock` and root `.venv` serve Python development. Root `pnpm-workspace.yaml` and `pnpm-lock.yaml` cover the `@evidence-lab/dashboard` package and shared Biome tooling. The dashboard package pins its TypeScript compiler. Task coordinates both stacks through the root and application Taskfiles; `tooling/` holds shared operational helpers.

The dashboard uses Vue 3 single-file components with TypeScript and Vite. Edit views in `apps/dashboard/src/views/`, reusable components in `src/components/`, workspace actions in `src/composables/`, validated HTTP access in `src/api/`, and styles in `src/assets/`. The root Vue component is `src/App.vue`; `public/` contains the favicon. `task dashboard:build` checks Vue templates and compiles hashed assets into the ignored `apps/dashboard/dist/` directory. FastAPI directly serves that build at `/` and `/static`; edit the TypeScript/public sources instead of generated output. `task dashboard:serve` starts the API with the built dashboard, as does `task app:serve`. API startup, CLI and application test tasks ensure the dashboard is built.

Docker builds the dashboard in a separate stage and copies its output to `/app/apps/dashboard/dist/`. The Python wheel contains the backend and migrations; browser assets remain a separate build output. `task build` builds both frontend assets and backend distributions. Serving an installed wheel requires the built `apps/dashboard/dist/` directory and the repository root as the working directory, which the Task wrappers use.

The dashboard retains the existing HTTP contracts and handwritten TypeScript types. There are no shared library consumers or cross-language generated schemas requiring `packages/`, `proto/` or `gen/` directories yet. Task is the single runner; a separate Turborepo runner would add coordination without serving this two-app structure.

Run all tasks from the repository root so configuration, fixture and artifact paths remain rooted here:

```bash
task setup         # install locked uv/pnpm dependencies and build the dashboard
task lock          # deliberately update workspace dependency locks
task lint          # Ruff, dashboard type checking and Biome lint
task format        # format Python with Ruff and frontend sources with Biome
task test          # Python and frontend tests; integration prerequisites apply
task check         # lint, then the complete test suite
task typecheck     # check dashboard TypeScript types
task build         # build dashboard assets, then the Python distribution
task clean         # remove generated development/build caches
```

`task --list` lists the available wrappers. Run `task dashboard:typecheck`, `task dashboard:lint`, `task dashboard:test` or `task dashboard:build` for frontend-only work; `task dashboard:serve` runs the dashboard with the API. `task test:offline` selects tests that do not require integration or native PostgreSQL. Full test execution must retain prerequisite skips honestly; a skipped native suite does not establish native concurrency or recovery behavior. Compose remains rooted at `compose.yaml`; `task compose:down` removes containers while preserving the named database volume. `RAG_CONFIG=./configs/private.yaml task compose:up` selects a private mounted YAML, and direct Compose commands remain supported for project-specific options.

## Repository map

| Path | Contents |
|---|---|
| `apps/evidence-lab/src/evidence_lab/config.py` | Strict YAML schema, active-profile validation and redaction |
| `apps/evidence-lab/src/evidence_lab/providers/` | OpenAI-compatible/native adapters, transport and fixture mode |
| `apps/evidence-lab/src/evidence_lab/storage.py`, `apps/evidence-lab/src/evidence_lab/migrations/` | PostgreSQL, vectors, immutable versions, leases and cost reservations |
| `apps/evidence-lab/src/evidence_lab/ingestion.py`, `retrieval.py` | Extraction, stable chunks, embedding cache, exact hybrid retrieval |
| `apps/evidence-lab/src/evidence_lab/engine.py`, `policy.py` | Frozen evidence, release decisions, repair and semantic identity |
| `apps/evidence-lab/src/evidence_lab/api.py`, `worker.py` | HTTP API and durable execution |
| `apps/dashboard/src/`, `apps/dashboard/public/` | TypeScript dashboard and editable HTML/CSS/favicon |
| `apps/dashboard/dist/` | Generated dashboard build served directly by FastAPI |
| `apps/evidence-lab/src/evidence_lab/evaluation.py`, `experiments.py` | Paired evaluation, independent studies and qualification evidence |
| `apps/evidence-lab/tests/` | Contract, integration and native PostgreSQL verification |
| `tooling/` | Alembic configuration and backup/restore scripts |
| `data/`, `configs/`, `docs/` | Shared fixtures, operator configuration and documentation |
| `pyproject.toml`, `uv.lock` | Python workspace, shared development tools and dependency lock |
| `package.json`, `pnpm-workspace.yaml`, `pnpm-lock.yaml`, `biome.json` | Frontend workspace, shared tooling and dependency lock |
| `Taskfile.yml`, application Taskfiles | Task orchestration for both stacks |

This is a bounded PoC, not a production rollout or a validated domain model. Stored documents may themselves be inaccurate or incomplete. Its empirical purpose is to determine whether a selected hosted verifier improves useful, supported answers under the measured policy, latency and cost constraints.

For dashboard hot reload, run `task app:serve` and `task dashboard:dev` in separate terminals. Open the Vite URL printed by the latter; its `/api` and `/health` requests proxy to the API on localhost port 8000. Use `task dashboard:serve` for the built dashboard served by FastAPI. See [dashboard development](apps/dashboard/README.md) for the component structure and test commands.
