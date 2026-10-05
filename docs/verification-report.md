# Implementation and verification report

Build date: **5 October 2026**. Version: **0.1.0**.

## Result

The application is implemented and its complete deterministic workflow runs: upload a document, extract and index it, retrieve evidence, generate a cited draft, verify the exact text, perform at most one fully checked repair, and release the result or abstain. The operator UI, durable worker, command-line tools, evaluation runner, repeatability/load tooling and recovery helpers are included.

**Final automated result: 359 passed, 0 failed, 0 errors, 4 skipped.** A separate installed-package walkthrough passed **11 checks**, and two real-browser suites passed **18 checks** with no browser errors. The skipped cases require native PostgreSQL concurrency or native backup/restore; they remain outstanding.

No hosted inference was performed. No model weights, tokenizer downloads or model-serving runtime are included. The bundled synthetic evaluation is explicitly unqualified, and its natural answer-quality fields remain pending human review.

## Implemented scope

| Area | Delivered behavior |
|---|---|
| Configuration | Strict YAML; complete endpoints, direct keys or explicit secret references, model roles, capabilities, limits, dated prices and phase budgets; safe validation errors and redacted provenance |
| Inference | OpenAI-compatible embeddings and chat adapters; optional native Clef adapter behind explicit protocol opt-in; no automatic provider fallback |
| Persistence | PostgreSQL and pgvector migrations, immutable source versions and chunks, native full-text search, embedding cache, durable jobs, traces and budget reservations |
| Ingestion | UTF-8 text, Markdown and readable PDFs; bounded extraction, visible OCR/review states, resumable embedding batches, atomic activation, explicit retry |
| Retrieval | Exact cosine and lexical candidates, rank fusion, overlap handling, corpus filters and a frozen source-version snapshot |
| Release | Structural checks; every answer block and three global checks; exact answer/evidence/round binding; one repair; explicit technical failure, shadow and abstention states |
| Operator interface | Upload/update/preview, corpus creation and selection, question history, immutable citations, evidence, separately labeled unverified trace, evaluation and JSON export |
| Evaluation | Shared-input A/B/C/D comparisons, controlled claims without repair, complete denominators, hash-bound review templates, uncertainty summaries and qualification gates |
| Independent experiments | Predeclared twenty-question verifier repeatability, three passes per question, four-concurrent-query API load, separate cold/warm and repair/no-repair strata |
| Operations | CLI, health checks without inference, lease recovery, retention and source purge, native backup/empty-target restore scripts, pinned dependencies and container references |

FactCG and MiniCheck remain deferred. They are not dependencies or locally hosted comparison models.

## Test environment and limits of the evidence

The final Python suite ran on **Python 3.12.14**, using the pinned `requirements.lock`. SQL integration used **PGlite PostgreSQL 18.3 with pgvector 0.8.1 through a PostgreSQL TCP connection**. This executes PostgreSQL/vector SQL in the temporary test environment; it does not establish native multi-session concurrency or native backup/restore behavior.

The delivered deployment uses native **PostgreSQL 17 with pgvector 0.8.2**, as pinned in `compose.yaml`. Docker and native PostgreSQL client/server tools were unavailable in the build environment. Container registry references were checked and recorded in [container-images.json](container-images.json), but a native Compose startup has not been executed. The exact deployment version is therefore an outstanding integration check.

The Python wheel was built, installed into a separate environment with the pinned dependencies, and exercised without importing the editable checkout. Migration, bundled data discovery and served static assets worked from that installation. All 28 packaged source/static files matched the final checkout byte for byte; no Python bytecode was packaged. See [packaging-checks.json](build-evidence/packaging-checks.json).

Ruff, JavaScript syntax checking and Python dependency compatibility checks passed. The test client emitted an upstream Starlette deprecation warning about its httpx adapter; it did not cause an application or test failure.

## Final automated suite

The fixed verification manifest includes all 13 project test files. It records individual outcomes and maps mandatory behavior to 16 fault areas. Missing or skipped requirements prevent a complete or qualifying fault artifact.

| Test group | Passed | Skipped |
|---|---:|---:|
| Configuration | 55 | 0 |
| Provider contracts and transport | 85 | 0 |
| Ingestion | 26 | 0 |
| Retrieval | 19 | 0 |
| PostgreSQL persistence | 36 | 3 |
| Answer engine and policy | 28 | 0 |
| API boundaries and SQL/worker integration | 24 | 0 |
| Evaluation | 32 | 0 |
| Worker recovery | 10 | 0 |
| Repeatability and load experiment contracts | 27 | 0 |
| Native backup/restore | 0 | 1 |
| Verification-artifact integrity | 15 | 0 |
| Streaming upload-body limits | 2 | 0 |
| **Total** | **359** | **4** |

The four skipped checks cover competing job claims, competing budget reservations, a consistent dense/lexical snapshot during concurrent source activation, and native `pg_dump`/`pg_restore` recovery of sources, vectors, traces and migration revision.

The final artifact is bound to the CLI demo evaluation and the unchanged implementation. Its flags correctly remain `complete: false`, `qualification_eligible: false`, `native_postgres_verified: false` and `restore_verified: false`.

- [Fault-suite JSON](build-evidence/verification/fault-suite-c9056565-6b41-4c49-85b2-995630b9bdf6.json)
- [JUnit results](build-evidence/verification/fault-suite-c9056565-6b41-4c49-85b2-995630b9bdf6.junit.xml)
- [Sanitized test transcript](build-evidence/verification/fault-suite-c9056565-6b41-4c49-85b2-995630b9bdf6.log.txt)

## Installed-package and browser workflows

The [installed-package record](build-evidence/cli-checks.json) contains eleven successful checks: configuration, migration, seeding, query release, retrieval, mock endpoint smoke, durable evaluation, evaluation export, API/static-asset serving, migration with an older embedding-space corpus, and creation of a new corpus after the space change.

The last two checks verify an operator transition found during deployment review. Changing the configured embedding space preserves the existing default corpus, lets the application start for inspection, and allows creation of a separate compatible corpus. Existing vectors are never relabeled as belonging to the new space.

The [main browser walkthrough](build-evidence/ui-browser-walkthrough.json) passed ten scenarios against the actual API and worker: empty-corpus abstention, multipart ingestion, inert rendering of uploaded HTML, cited answer release, exact source-version navigation, one repair with complete recheck, persistent rejection, historical source preservation, durable evaluation/export and mobile layout.

The [corpus/retry walkthrough](build-evidence/ui-corpus-retry.json) passed eight further scenarios. It checked corpus-ID validation, empty-corpus isolation, selected-corpus retrieval, filtered history/counts, mismatch guidance and mobile selection. A test-only one-time embedding failure produced a recoverable ingestion error; the browser retried the same durable job, which succeeded on attempt two. The failed notification then changed to Complete.

Both browser reports include final static-file hashes and zero browser errors. Reviewed screenshots are included alongside them: [answer](build-evidence/ui-answer.png), [documents](build-evidence/ui-documents.png), [evaluation](build-evidence/ui-evaluation.png), [recoverable failure](build-evidence/ui-recoverable-failure.png), [successful retry](build-evidence/ui-retry-complete.png) and [mobile corpus selector](build-evidence/ui-corpus-mobile.png).

## Synthetic evaluation result

The final exported demo completed all **seven questions and sixteen controlled cases**. It used deterministic fixtures with **zero remote provider attempts**. Four fictional documents were also seeded through the ordinary ingestion path for the separate retrieval walkthrough.

| Variant | Released fixture answers | Abstentions | Declared questions |
|---|---:|---:|---:|
| A: ungated candidate | 6 | 1 | 7 |
| B: structural checks | 6 | 1 | 7 |
| C: semantic gate | 4 | 3 | 7 |
| D: gate plus one repair | 6 | 1 | 7 |

These are fixture workflow outcomes, not model-quality measurements. Eleven distinct answers await human review. Correctness, completeness, natural unsupported-release rates and conflict-handling quality remain unestablished. The inline-evidence questions do not measure retrieval quality.

The complete deliverables are [report.json](build-evidence/cli-demo/report.json), [questions.csv](build-evidence/cli-demo/questions.csv), [controlled.csv](build-evidence/cli-demo/controlled.csv), [summary.md](build-evidence/cli-demo/summary.md) and the deliberately unfilled [answer-review-template.json](build-evidence/cli-demo/answer-review-template.json). Diagnostic drafts in these files are operator review material.

The report and fault artifact share implementation fingerprint:

```text
9033cf3e02367ba0edb0acbc77065499b092321285e89dd14ac3a717df9ca4df
```

## Remaining acceptance work

The implementation is ready for the native host and selected endpoints to be exercised. The remaining work depends on resources not supplied for this build:

1. Start the pinned Compose stack and run the mock seed/query walkthrough on the target host.
2. Run native verification against an explicitly supplied test administrator DSN, with native PostgreSQL client tools available. The harness creates and removes its own disposable database; use the README command and inspect all four native outcomes.
3. Fill the private YAML with the embedding, generation and verification endpoints, keys, model IDs, capabilities, prices and positive phase budgets. Start in shadow mode, then run the explicit endpoint smoke command.
4. Supply the actual corpus and independently reviewed development/held-out gold. Freeze policy and selected profiles, execute the paired study, complete blind answer review, and run the predeclared live repeatability/load experiments.
5. Run policy qualification with matching evaluation, native fault, load and repeatability artifacts. Enable live gated release only after qualification succeeds.

The build supplies the commands, schemas, review rubric and gate logic for these steps. It supplies no fabricated human labels, hosted benchmark results or qualified live policy. Full setup commands are in [README.md](../README.md); study instructions are in [evaluation.md](evaluation.md) and [experiments.md](experiments.md).
