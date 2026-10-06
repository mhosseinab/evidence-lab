# Agent RAG MCP verification

Verified 6 October 2026. The implementation is a private trusted-operator MCP
interface using stateless Streamable HTTP at `/api/mcp/`, with `search_evidence`
and `get_evidence_source`. It does not implement OAuth delegation or per-agent
identities. The global corpus grant and explicit Host allowlist are configurable.
The operator token remains a full operator credential outside the MCP tools.

## Repository checks

Run from the repository root:

```sh
UV_CACHE_DIR=/tmp/evidence-mcp-uv task setup
EVIDENCE_LAB_TEST_DSN=postgresql://evidence:evidence@127.0.0.1:5432/evidence_mcp_test_20261006 task check
UV_CACHE_DIR=/tmp/evidence-mcp-uv task build
git diff --check
```

Setup, lint, Python/dashboard types, packaging and whitespace checks passed.
The full suite passed **524 Python tests and 56 dashboard tests**, with one
native backup/restore prerequisite skip (`EVIDENCE_LAB_TEST_NATIVE_ADMIN_DSN`
unset). Dedicated PostgreSQL private-schema fixtures supplied isolation; the
test database was removed after verification. Dashboard totals include the
workspace's deployment proxy tests.

MCP-specific tests cover 17 cases: actual SDK client discovery over ASGI HTTP,
2026-07-28 stateless protocol requests and 2025-11-25 initialization, the exact
two-tool allowlist and output schemas, operator-token enforcement in fixture
mode, wrong tokens, hostile Origin/Host, explicit remote-host configuration,
corpus grants/input limits, oversized HTTP bodies/results, safe internal/budget
errors, native pgvector citations and empty results, cross-corpus source rejection,
embedding-space mismatch before inference and source deletion. The initialization
test failed with 404 before implementation.

Wheel inspection confirmed `evidence_lab/mcp_server.py` is included. Three
generated `/static/` asset URLs from the built index returned HTTP 200 from
FastAPI. No dashboard source was changed for this feature.

## Isolated deployment plumbing

A uniquely named Compose project, `evidence-mcp-verification-20261006`, used the
existing Compose topology with a fixture override stored at
`/tmp/evidence-mcp-compose/compose.yaml`. It used a uniquely tagged image, temporary
in-container tmpfs storage, no published database port, and a dynamically assigned
API port bound only to loopback. The runtime config copied public mock Compose
settings and added a dummy operator token; no deployed private config was read.

Commands:

```sh
EVIDENCE_LAB_CONFIG=/tmp/evidence-mcp-compose/runtime.yaml docker compose -p evidence-mcp-verification-20261006 -f compose.yaml -f /tmp/evidence-mcp-compose/compose.yaml up --build --wait api worker
EVIDENCE_LAB_CONFIG=/tmp/evidence-mcp-compose/runtime.yaml docker compose -p evidence-mcp-verification-20261006 -f compose.yaml -f /tmp/evidence-mcp-compose/compose.yaml exec -T api evidence-lab seed --config /app/configs/runtime.yaml --wait
.venv/bin/python /tmp/evidence-mcp-compose/check.py
EVIDENCE_LAB_CONFIG=/tmp/evidence-mcp-compose/runtime.yaml docker compose -p evidence-mcp-verification-20261006 -f compose.yaml -f /tmp/evidence-mcp-compose/compose.yaml down
```

Build, migrations, health readiness, worker and fixture seeding passed. A real SDK
client connected over the loopback TCP port. It discovered exactly two tools,
retrieved four fixture excerpts, fetched a cited source with the same text hash,
and observed HTTP 401 without authentication. Verification scripts/fixture
configuration remain under `/tmp` for reproduction; a fresh run must update the
script's endpoint to the port reported by Compose. Temporary containers and their
network were removed with `down` without volume-deletion flags. Existing application
containers, databases and volumes were preserved; build cache was retained.

## Limits and logs

## Origin allowlist review fix

The parent API middleware now delegates authenticated MCP Origin checks to the
SDK allowlist while retaining same-origin protection for REST writes. The new
regression failed before the fix and passes afterward. It verifies an allowed
cross-origin MCP request, rejection of wrong tokens and unlisted origins, and
rejection of a cross-origin REST write before mutation.

Verification commands:

```sh
UV_CACHE_DIR=/tmp/evidence-mcp-uv task setup
.venv/bin/pytest -q apps/evidence-lab/tests/test_mcp_api.py apps/evidence-lab/tests/test_api.py -m 'not integration and not native_postgres'
EVIDENCE_LAB_TEST_DSN=postgresql://evidence:evidence@127.0.0.1:5432/evidence_mcp_origin_test_20261006 task check
git diff --check
```

Setup and diff checks passed. Focused tests: 44 passed, 2 deselected. Full checks:
525 Python tests passed, 1 prerequisite skip; 56 dashboard tests passed. The
dedicated test database was removed afterward. Logs are
`/tmp/evidence-mcp-origin-setup.log` and `/tmp/evidence-mcp-origin-check.log`.

## Verification limitations

No real keys, paid calls, live model inference, model hosting, public endpoint
deployment or external-agent authentication setup was used. Fixture retrieval
and transport checks are engineering evidence, not evidence of retrieval/model
quality. Remote HTTPS/proxy correctness must be verified in the target deployment.

Logs remain local:

- `/tmp/evidence-mcp-setup.log`
- `/tmp/evidence-mcp-check.log`
- `/tmp/evidence-mcp-build.log`
- `/tmp/evidence-mcp-compose-build.log`
- `/tmp/evidence-mcp-compose-seed.log`
- `/tmp/evidence-mcp-compose-wire.log`
- `/tmp/evidence-mcp-compose-cleanup.log`
