# Grounded RAG PoC

An end-to-end, document-grounded question-answering application with a bounded verification gate. It accepts documents, stores immutable sources, retrieves evidence, creates a cited draft, verifies that exact draft and either releases it, repairs it once, or abstains. The application and PostgreSQL run on your infrastructure. Real inference uses your configured external endpoints.

**No model server, GPU, model weights or tokenizer downloads are included.** The default demo uses deterministic fixtures and makes no inference requests. Its answers are labeled `fixture_only`; they demonstrate application behavior, not model accuracy.

The approved design is in [docs/implementation-plan.md](docs/implementation-plan.md). Actual verification results and remaining deployment checks are in [docs/verification-report.md](docs/verification-report.md).

## Start the complete demo

Requirements: Docker with Compose. Run these commands from this directory:

```bash
docker compose up --build -d
docker compose exec api rag-poc seed --config /app/configs/runtime.yaml --wait
```

Open **http://127.0.0.1:8000**. The Documents view shows each uploaded source and processing state. The Ask view displays the released answer, immutable citations and retrieved evidence. The Evaluations view runs the bundled synthetic A/B/C/D comparison and exports its raw result.

The Compose stack contains PostgreSQL/pgvector, a migration job, an API and a worker. The PostgreSQL and HTTP ports bind to localhost. The demo database credentials are `rag:rag`; they are local sample values. Images are pinned by digest in `Dockerfile` and `compose.yaml`, with their registry metadata in `docs/container-images.json`.

For a small, explicit walkthrough, upload a text file containing:

```text
The Atlas refund period is 30 days after purchase.
Refund requests require the order identifier.
```

Ask `What is the Atlas refund period in days?`. Open the citation to inspect the exact source version. In mock mode, adding `[fixture:unsupported]` to the question deliberately inserts an unsupported initial claim; the worker rejects it, repairs once and verifies the entire replacement. Inspect the separately labeled operator trace to see both rounds. `[fixture:conflict]` exercises persistent rejection and abstention. These markers are fixture controls, not features of live models.

Stop processes with `docker compose stop`. This preserves the database volume. The source, vectors, job records, traces and evaluation reports are stored in PostgreSQL.

## Local Python development

Use Python 3.12 or newer and a PostgreSQL server with pgvector installed. The local configuration defaults to `postgresql://rag:rag@localhost:5432/rag`. You can run just the database from Compose:

```bash
docker compose up -d db
python -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/rag-poc migrate --config configs/mock.yaml
.venv/bin/rag-poc seed --config configs/mock.yaml --wait
```

Start the API and worker in separate terminals:

```bash
.venv/bin/rag-poc serve --config configs/mock.yaml
.venv/bin/rag-poc worker --config configs/mock.yaml
```

The worker processes up to four jobs concurrently by default. A persistent reservation ledger enforces the configured outbound concurrency across worker processes. PostgreSQL leases fence every worker publication, and a restart cannot reset remote attempt or cost accounting. The `--once` worker option processes at most one job for debugging.

## Configure your providers

Copy `configs/live.example.yaml` to `configs/private.yaml`. Supply complete values for the three active roles: `embeddings`, `generator` and `verifier`. The example intentionally fails validation until its placeholders are replaced.

```bash
cp configs/live.example.yaml configs/private.yaml
.venv/bin/rag-poc config-check --config configs/private.yaml
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
.venv/bin/rag-poc smoke --config configs/private.yaml
.venv/bin/rag-poc smoke --config configs/private.yaml --execute
```

The first command is a dry run. The second sends one embedding request, one draft request and one batch verification request, subject to retries and budgets. A smoke pass establishes basic endpoint compatibility, not domain quality.

### Optional Cloudflare Clef

Clef is an explicit native adapter. Set `runtime.require_openai_compatible: false`, complete the inactive `clef_full` profile, and select it as `roles.verifier`. Its full operation URL ends in `@cf/cloudflare/clef`; the native body model is `clef`. The adapter implements the documented state/question/choice contract and validates complete question coverage and probability distributions. It does not route Clef through Chat Completions.

Before a paid comparison, recheck access, the current model contract and prices against the provider. Select any native probability threshold on development data and freeze it before held-out testing. These native values are not calibrated guarantees. FactCG and MiniCheck remain deferred, as requested; neither is downloaded or hosted.

### Move from mock to live storage

Mock and live embeddings have different space identities. Changing endpoint/model/space/dimensions cannot silently mix vectors with an existing corpus. Create a new corpus with the workspace selector and re-upload your original sources under the new embedding configuration. Older corpora remain available for source inspection; queries against a mismatched space fail explicitly before inference. You can also use a separate database or Compose project, preserving the old volume. Update the private YAML DSN to the chosen database. Inside the supplied Compose network the DB hostname is `db`.

To run a private configuration in Compose:

```bash
RAG_CONFIG=./configs/private.yaml docker compose up --build -d
```

For a separate live Compose project, stop the demo processes to free the local ports and use `docker compose -p grounded-rag-live ...`. Keep endpoint and credential changes in the mounted private YAML. Restart the API and worker after configuration changes. Optional `runtime.operator_token` protects all `/api/` operations; the UI has an operator-token entry dialog.

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
.venv/bin/rag-poc evaluate --config configs/mock.yaml
.venv/bin/rag-poc evaluate --config configs/mock.yaml --execute --output artifacts/demo
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
.venv/bin/rag-poc offline-tests --config configs/mock.yaml
.venv/bin/ruff check src tests scripts
```

Create a machine-readable fault artifact with `rag-poc verify --config configs/mock.yaml --output artifacts/verification`. For a qualifying native run, explicitly provide a test administrator connection in `RAG_TEST_NATIVE_ADMIN_DSN` and add `--native --report PATH/TO/REVIEWED_REPORT.json`. The runner creates a uniquely named disposable database; it never selects the application database as a test target. It also needs native PostgreSQL client tools for the restore test. Missing prerequisites remain recorded as skipped checks, and `--native` exits nonzero when its required checks are incomplete.

After the actual held-out review, fault suite and independent experiments are complete, assess them together:

```bash
.venv/bin/rag-poc qualify --config configs/private.yaml artifacts/reviewed-evaluation/report.json \
  --fault-artifact artifacts/fault-suite.json \
  --load-artifact artifacts/live-load.json \
  --repeatability-artifact artifacts/repeatability.json \
  --policy-id heldout-policy-v1 --output policies/heldout-policy-v1.json
```

Use the actual artifact paths returned by each command. A failed qualification writes the unmet requirements and exits nonzero. Only a passing artifact can enable live `verification.mode: gated` with matching `policy_id` and `policy_path`. Changing code, verification profiles, prompts, packing rules or measured runtime limits invalidates the match. Rotating a key does not. `rag-poc experiments -- --help` shows the repeatability/load commands; each execution command defaults to a dry run and requires `--execute` to submit work.

The tests cover malformed and reordered embeddings, duplicate/foreign verdicts, native Clef protocol opt-in, timeouts/retries/cancellation, exact evidence/answer/round binding, one repair, private traces, source snapshots, version activation, quotas and durable ledgers. Integration tests require an explicitly selected disposable PostgreSQL test environment. Native multi-session concurrency and pg_dump/restore are separate required checks; they are not inferred from mock tests.

Health endpoints `/health/live` and `/health/ready` perform no inference. `/api/status` reports selected profile names, mode, policy state, limits and budget accounting without keys. Stage timing includes queue time; the query deadline is measured from acceptance. Cleanup uses configured retention and preserves immutable sources referenced by retained runs.

Native backup and empty-target restore helpers are included:

```bash
.venv/bin/python scripts/backup.py --config configs/private.yaml --output artifacts/backup.dump
.venv/bin/python scripts/restore.py --config configs/private-restore.yaml --input artifacts/backup.dump
```

Install matching PostgreSQL `pg_dump`/`pg_restore` clients on the operator host. The restore configuration must point to a new empty database with pgvector available. The helper refuses existing user objects and never runs `--clean`; it does not invoke models or re-embed sources. Back up private configuration separately from the database. A source deletion purges affected retained evidence/traces and cancels dependent jobs, so export required audit material before deliberately deleting a source.

`rag-poc --help` lists migration, upload, query, retrieval, worker, smoke, evaluation, trace and retention commands. Every command accepts `--config PATH` before or after its subcommand.

## Repository map

| Path | Contents |
|---|---|
| `src/rag_poc/config.py` | Strict YAML schema, active-profile validation and redaction |
| `src/rag_poc/providers/` | OpenAI-compatible/native adapters, transport and fixture mode |
| `src/rag_poc/storage.py`, `migrations/` | PostgreSQL, vectors, immutable versions, leases and cost reservations |
| `src/rag_poc/ingestion.py`, `retrieval.py` | Extraction, stable chunks, embedding cache, exact hybrid retrieval |
| `src/rag_poc/engine.py`, `policy.py` | Frozen evidence, release decisions, repair and semantic identity |
| `src/rag_poc/api.py`, `worker.py`, `static/` | HTTP API, durable execution and operator UI |
| `src/rag_poc/evaluation.py`, `experiments.py`, `verification.py` | Paired evaluation, independent studies and qualification evidence |
| `data/`, `tests/`, `scripts/` | Fixtures, review inputs, focused verification and recovery helpers |

This is a bounded PoC, not a production rollout or a validated domain model. Stored documents may themselves be inaccurate or incomplete. Its empirical purpose is to determine whether a selected hosted verifier improves useful, supported answers under the measured policy, latency and cost constraints.
