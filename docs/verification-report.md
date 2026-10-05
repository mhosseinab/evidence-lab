# Evidence Lab Vue migration verification

Verified on **5 October 2026**. The dashboard now uses Vue 3 single-file components, TypeScript and Vite, with the existing FastAPI single-origin deployment.

## Structure and behavior

`apps/dashboard/src/main.ts` mounts `App.vue`. `views/` contains Ask, Documents and Evaluations; `components/` contains layout, dialogs, answers, evidence, document tables and metric tables. `composables/` owns reactive workspace state, typed context, requests and polling lifecycle. `api/client.ts` validates HTTP responses; `types/api.ts` describes payloads; `utils/` contains presentation and release helpers; `assets/` contains the existing visual styles. The HTML entry is `apps/dashboard/index.html`; `public/` retains the favicon.

The legacy imperative dashboard, public HTML entry and custom compiler/copy script are removed. Vue interpolation renders untrusted source and answer text literally. Answer fields remain hidden for running, failed, shadow, unqualified, cancelled and abstained states. The operator token stays in memory. Each workspace owns its state; disposal aborts HTTP requests, resolves/clears polling timers, revokes download URLs and erases the token. Request generations prevent stale corpus, run, evaluation, source and trace responses replacing newer state. Upload revisions retain the selected document's corpus when the file picker opens. Later document refreshes recover upload notices after polling failures.

Vite emits content-hashed JavaScript/CSS under `apps/dashboard/dist/assets`. FastAPI serves generated HTML at `/` and assets at `/static`. Startup checks require the HTML, favicon, JavaScript and CSS; implementation identity includes every generated asset so additional Vue chunks cannot escape identity tracking. Task and Docker retain the same workspace build path; no dashboard files are copied into the Python package. `task dashboard:dev` adds Vite hot reload with a local API proxy.

Strict `vue-tsc` checks include templates, component props, source and tests. Vue 3.5.43, Vite 8.3.2, vue-tsc 3.3.12, Vitest 5.0.3, Vue Test Utils 2.5.1 and jsdom 30.1.2 are locked. TypeScript 6.0.3 is pinned because the current vue-tsc release requires compiler APIs not exported by TypeScript 7. Biome 2.5.15 checks Vue markup and scripts with full HTML support enabled. Existing two CSS cascade exceptions remain; one scoped tabindex exception preserves keyboard scrolling of the operator trace.

## Observed checks

| Check | Result |
|---|---|
| Locked uv/pnpm installation, `uv lock --check`, `uv pip check` | Passed |
| `task check` with dedicated native PostgreSQL DSNs | **350 Python + 24 Vue/API tests passed; no failures or skips** |
| `task test:offline` | **317 Python + 24 Vue/API tests passed; 33 database cases deselected** |
| Ruff, strict Vue/TypeScript and Biome | Passed |
| Requested Python test cleanup | Both API/evaluation files pass Ruff lint and formatting; their 58 tests passed against fresh native PostgreSQL |
| `task build` | Dashboard, wheel and source distribution built |
| Isolated wheel installation | Correct imports, migrations, no legacy static/runner/bytecode; generated hashed assets served correctly |
| Docker image build | Passed with the Vite build in the Node stage |
| Isolated Compose startup | Database, migration, API and worker started; migration exited successfully and demo seeding passed |
| Actual Task and Compose serving | HTML, JavaScript, CSS and favicon match local build bytes; readiness passed for both services |
| Browser | Four seeded documents, completed question with citation focus/source preview, completed seven-question evaluation with metrics and qualification limitations |
| Responsive browser check | At 390 px, no page horizontal overflow |
| Vite hot reload | Task starts development server; HTML/HMR entry and Vue SFC transformation passed |
| Multipart HTTP upload | Accepted with HTTP 202 through both Task and Compose services |
| Approved implementation plan | Unchanged; SHA-256 `7492b09e9f4c3abc56940a34644b9d1be3c70028d93bdb6fba775c464ec12b26` |

Browser navigation, queries, source previews and evaluation produced no console errors before the upload exercise. The browser tool's multipart requests failed with `ERR_ALPN_NEGOTIATION_FAILED` against both services, and the UI displayed an actionable connection error. Direct multipart HTTP requests succeeded against both endpoints; multipart boundary/token handling, revision corpus capture, upload validation and status recovery passed automated tests. End-to-end upload completion through that browser tool remains unverified.

The Python suite emits one upstream Starlette/httpx deprecation warning. Checks used Python 3.12.14, native PostgreSQL 17/pgvector 0.8.2, Task 3.53.1, uv 0.12.17, Node 24.21.0 and pnpm 12.6.0. Existing deployment services/data were preserved. Disposable verification resources and generated outputs were cleaned after evidence capture; installed dependencies remain. Startup/build tasks recreate `dist`.

## Project guidance

`AGENTS.md` now describes the Vue structure, coordination and cleanup rules. Four repository-local skills cover development, dashboard work, engineering verification and security review. Four Codex role definitions cover backend, dashboard, verification and security review. Skill validators and TOML/YAML parsing passed; Codex prompt diagnostics confirmed skill discovery. Start a new Codex session to refresh available skills and roles. These files supply workflows and ownership conventions without changing global settings or permissions. Creating the security-review guidance did not perform a security audit.

## Evidence and reproduction

- [Full Task check transcript](build-evidence/vue-dashboard/task-check.log.txt)
- [Native JUnit results](build-evidence/vue-dashboard/tests.junit.xml)
- [Offline transcript](build-evidence/vue-dashboard/task-offline.log.txt)
- [API/evaluation tests after formatting](build-evidence/vue-dashboard/python-focused.log.txt)
- [Dashboard development structure](../apps/dashboard/README.md)

Historical artifacts retain their original names, paths and hashes; they are not current qualification evidence. Git supplies source history; the removed build manifest and verification runner remain absent.

From the checkout root:

```bash
task setup
task test:offline
task lint
task build
task dashboard:serve
```

For hot reload, run `task app:serve` and `task dashboard:dev` in separate terminals, then open Vite's printed URL. For a container deployment, use `task compose:up` and `task compose:seed`; first follow the README's instructions if existing services occupy the configured ports.

For all native tests, set `RAG_TEST_DSN` and `RAG_TEST_NATIVE_ADMIN_DSN` to dedicated PostgreSQL/pgvector test connections and run `task check`. Native restore tests require PostgreSQL client tools. Run `task clean` after checks to remove generated outputs.

No model downloads, hosted inference or paid calls were performed. Mock workflows do not establish model accuracy. Human-reviewed held-out gold, real endpoint performance and a qualified live release policy remain unmeasured; this migration does not establish production readiness.


## Python type-diagnostic follow-up — 6 October 2026

Added explicit non-None assertions before indexing worker results and Wilson intervals or comparing the zero-event bound. The API test double's mutable run mapping and evaluation artifact/sample fixtures now have explicit types, resolving the remaining inferred-container errors in these two files.

Basedpyright analyzed both files with the root Python environment: **zero errors**. Its default strict diagnostics still report 803 warnings, mostly unannotated fixture/unknown-type warnings; this change does not claim a warning-free repository. Ruff lint and formatting checks passed, and all **58 tests** in the two files passed against a fresh native PostgreSQL fixture. Temporary tools, database and generated caches were removed after checking.
