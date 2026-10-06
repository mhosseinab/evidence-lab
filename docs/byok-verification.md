# BYOK engineering verification

Verified 6 October 2026. No real keys, live inference or paid provider calls were used.

The dashboard manages fixture/live mode, exactly two key groups (LLM and
Cloudflare), complete embedding/chat endpoint URLs and model identifiers,
Cloudflare account ID, limits, output compatibility, prices and budget.
Keys and preferences live only in browser local storage, scoped to the base
configuration. Keys accompany inference POSTs only; API tasks discard scoped
keys after completion. PostgreSQL stores job metadata and results, never keys.
Browser jobs are marked atomically and excluded from general workers.

## Results

| Command | Result |
| --- | --- |
| `UV_CACHE_DIR=/tmp/evidence-byok-uv task setup` | Passed using a writable temporary uv cache. |
| `EVIDENCE_LAB_TEST_DSN=postgresql://evidence:evidence@127.0.0.1:5432/evidence_byok_modes_test_20261006 task check` | Lint and type checks passed; Python 465 passed, 1 skipped; dashboard 41 passed. |
| `EVIDENCE_LAB_TEST_DSN=postgresql://evidence:evidence@127.0.0.1:5432/evidence_byok_modes_test_20261006 .venv/bin/pytest apps/evidence-lab/tests/test_dashboard_runtime_api.py apps/evidence-lab/tests/test_byok.py apps/evidence-lab/tests/test_storage.py -q` | 66 passed. Native PostgreSQL tests cover job isolation and custom live ingestion with deterministic HTTP authentication fixtures. |
| `UV_CACHE_DIR=/tmp/evidence-byok-uv task build` | Passed; Vue dashboard, Python wheel and source distribution built. |
| `git diff --check` | Passed. |

The first setup build ran while dashboard edits were incomplete and failed its
type check. After those edits finished, setup, the full check and build passed.

The one skipped test is native backup/restore, requiring explicit
`EVIDENCE_LAB_TEST_NATIVE_ADMIN_DSN`. All applicable native BYOK tests ran using
a dedicated PostgreSQL database and private schemas. The fixture database was
removed after verification; existing application data and deployments were preserved.

Browser verification at `http://127.0.0.1:5173/static/` confirmed visible mode and
both key fields, saved endpoint/model settings, successful switch to live with
matching model metadata, preference restoration after reload, and switch back
to fixture mode. Zero budget and no saved keys ensured no inference occurred.
The temporary setup was removed through its dashboard control. The dashboard
was left in fixture mode with blank setup and keys. Screenshot:
`/tmp/evidence-byok-modes.jpg`.

To reproduce with the existing local demo database container:

```bash
docker exec evidence-lab-db-1 createdb -U evidence evidence_byok_modes_test_20261006
EVIDENCE_LAB_TEST_DSN=postgresql://evidence:evidence@127.0.0.1:5432/evidence_byok_modes_test_20261006 task check
EVIDENCE_LAB_TEST_DSN=postgresql://evidence:evidence@127.0.0.1:5432/evidence_byok_modes_test_20261006 .venv/bin/pytest apps/evidence-lab/tests/test_dashboard_runtime_api.py apps/evidence-lab/tests/test_byok.py apps/evidence-lab/tests/test_storage.py -q
UV_CACHE_DIR=/tmp/evidence-byok-uv task build
docker exec evidence-lab-db-1 dropdb -U evidence evidence_byok_modes_test_20261006
```

## Limits

API restarts interrupt browser-key work. Recovery requires browser credentials
again. Unattended CLI/background work uses existing server-key configuration.
Live endpoint compatibility, performance and model quality remain unmeasured.
The live browser setup uses an unqualified shadow policy, and embedding changes
require a new corpus and re-uploaded sources. A zero budget blocks live calls.

Execution logs are outside source documentation:
`/tmp/evidence-byok-modes-check.log`, `/tmp/byok-modes-native.log` and
`/tmp/evidence-byok-modes-build.log` and `/tmp/evidence-byok-modes-setup.log`.

## Model defaults and required-field follow-up

Verified 6 October 2026. Arbitrary provider/model limits cannot be inferred from
an OpenAI-compatible endpoint. Embedding dimensions, all model token limits,
the output-limit parameter and provider prices now start unset and require
explicit values. The safe initial budget is zero; plain JSON text sends no
`response_format` parameter. Required live fields and both provider keys have
visible asterisks. Existing saved setup is preserved.

[Cloudflare's Clef documentation](https://developers.cloudflare.com/workers-ai/models/clef/)
confirmed the fixed verifier defaults: 65,536-token context and $0.24 per million
input tokens. The form displays those values and links to the source.
Vue documentation confirmed empty numeric inputs remain empty strings; form
validation rejects them before numeric conversion or preference persistence.

`UV_CACHE_DIR=/tmp/evidence-byok-uv task setup` passed.
`EVIDENCE_LAB_TEST_DSN=postgresql://evidence:evidence@127.0.0.1:5432/evidence_byok_defaults_test_20261006 task check`
passed: 465 Python tests, 42 dashboard tests, one backup/restore prerequisite
skip. `UV_CACHE_DIR=/tmp/evidence-byok-uv task build` and `git diff --check` passed.
The dedicated database was removed afterward. Browser verification confirmed
blank model limits/prices, zero budget, initial JSON text, unselected token
parameter and required labels. Screenshot: `/tmp/evidence-byok-defaults.jpg`.
No keys were entered and no provider calls were made. Logs:
`/tmp/evidence-byok-defaults-check.log`, `/tmp/evidence-byok-defaults-build.log`.

## Operator sign-in and workspace-vector reuse

Verified 6 October 2026. Operator sign-in now has a separate header button and
dialog. Validated sign-in adopts the server configuration and omits browser
mode/setup/key headers; failed sign-in restores previous state. Sign-out clears
the page-memory token and restores browser preferences where access allows.
Tests cover those behaviors. Browser plumbing was exercised with a dummy token
on the unprotected local fixture server, then signed out; no real credential was
used. The signed-in connection view hides browser overrides. Screenshot:
`/tmp/evidence-operator-server.jpg`.

Live BYOK can select `embedding_source: workspace` to keep the base embedding
profile, execution mode and exact vector manifest. Compatible existing pgvector
corpora remain searchable without re-upload. `custom` remains the default for
older setup. Native PostgreSQL tests confirm corpus/vector rows remain unchanged,
query vectors match fixture embeddings, embedding HTTP is absent, and fake live
chat/Clef transport is used. This remains deterministic plumbing evidence,
not evidence of model quality. External workspace embeddings use the browser LLM
key; mixed server/browser provider credentials are not supported.

`EVIDENCE_LAB_TEST_DSN=postgresql://evidence:evidence@127.0.0.1:5432/evidence_operator_vectors_test_20261006 task check`
passed: **474 Python tests, 48 dashboard tests**, one native backup/restore
prerequisite skip. `UV_CACHE_DIR=/tmp/evidence-byok-uv task build` and
`git diff --check` passed. The dedicated database was removed. Logs:
`/tmp/evidence-operator-vectors-check.log`, `/tmp/evidence-operator-vectors-build.log`.

Browser verification kept four existing ready documents in live mode, with the
same fixture embedding model, no embedding-space mismatch, and visible fixture
retrieval warning. Screenshot: `/tmp/evidence-live-pgvector.jpg`. Zero-budget test
setup was removed afterward; the workspace was returned to fixture mode. No
real keys, model hosting, live inference or paid calls were used.

## Review bug fixes

Verified 6 October 2026. Server retries explicitly clear browser job ownership,
allowing ordinary workers to claim failed browser-origin ingestion and evaluation
jobs after operator sign-in. Live configuration with fixture embeddings skips
unused embedding key references. Four new regression cases failed before the
fixes and passed afterward, covering both job kinds and missing environment/file
key references.

Commands run from the repository root:

```sh
UV_CACHE_DIR=/tmp/evidence-byok-uv task setup
EVIDENCE_LAB_TEST_DSN=postgresql://evidence:evidence@127.0.0.1:5432/evidence_byok_fixes_test_20261006 .venv/bin/pytest -q apps/evidence-lab/tests/test_config.py apps/evidence-lab/tests/test_dashboard_runtime_api.py apps/evidence-lab/tests/test_byok.py apps/evidence-lab/tests/test_api.py
EVIDENCE_LAB_TEST_DSN=postgresql://evidence:evidence@127.0.0.1:5432/evidence_byok_fixes_test_20261006 task check
git diff --check
```

Setup passed, focused tests passed **114**, and the full check passed **478 Python
tests and 48 dashboard tests** with one native backup/restore prerequisite skip
(`EVIDENCE_LAB_TEST_NATIVE_ADMIN_DSN` unset). Logs:
`/tmp/evidence-byok-fixes-setup.log`, `/tmp/evidence-byok-fixes-check.log`.
The dedicated test database was removed. No real inference or paid calls were made.
