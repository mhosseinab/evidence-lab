# BYOK and server-credential security review

Reviewed 6 October 2026. Scope: current BYOK/dashboard changes and the server-key
authorization safeguard, including request configuration, uploads, queued jobs,
provider transport, diagnostics, and deployment boundaries. Product source was
read-only during this review.

## Confirmed finding

### EL-SEC-001 — [P2] Empty operator token bypasses server-key authorization

**Status:** remediated after the review at the user's request. The evidence below
records the pre-fix behavior. `RuntimeConfig.nonempty_operator_token` now rejects
blank tokens, and `api.py` rejects absent/blank tokens before server inference even
for explicit config objects. Regression coverage includes empty/whitespace tokens
across all five inference routes and three invalid YAML token cases. All 13 new
cases failed before the fix and passed afterward.

**Severity:** medium; high impact if a live server-credential deployment with an
empty token is reachable by untrusted callers. This is a configuration-dependent
bypass, not evidence of a bypass of the user's deployed non-empty token.

**Affected locations:**

- `apps/evidence-lab/src/evidence_lab/config.py:64`, `RuntimeConfig.operator_token`
  accepts an empty `SecretStr`; no non-empty constraint is configured.
- `apps/evidence-lab/src/evidence_lab/api.py:133`, `operator_boundary`, authenticates
  only when `if token` is true. An empty `SecretStr` is false.
- `apps/evidence-lab/src/evidence_lab/api.py:188`, `browser_config`, rejects only
  `operator_token is None`. An empty `SecretStr` is not `None`.

**Preconditions:** operator configuration uses `runtime.mode: live`, server
credentials, and `operator_token: ""`; an attacker can reach the API. Positive
server budgets and usable provider configuration are additionally required for
paid inference. Omitted tokens (`null`), absent bearer headers with a non-empty
configured token, and incorrect bearer headers are already rejected.

**Evidence:** a fully validated `AppConfig` with the empty token accepted
`POST /api/queries` without Authorization and created a queued server-owned run.
Observed result: `validated_empty_token=True`, `unauthenticated_status=202`,
`queued=True`. The reproduction used only dummy secrets, `APIStore` and
`NoInferenceHub`; it made no provider calls and modified no application data.

**Impact:** the two guards disagree about whether a token is configured, allowing
unauthenticated access to server-funded inference and other API operations under
this configuration. Spending is still subject to the persistent server ledger
caps; the issue is unauthorized use within those caps.

**Minimal fix:** reject empty/whitespace-only configured tokens at validation and
make the inference guard reject a missing or empty token consistently. Add
regression coverage for an empty token across the five inference routes, alongside
the existing absent/wrong/valid token cases. Do not rely on dashboard sign-in
validation as the authorization boundary.

**Safe reproduction from repository root:**

```sh
PYTHONPATH=apps/evidence-lab/tests .venv/bin/python - <<'PY'
from pydantic import SecretStr
from fastapi.testclient import TestClient
from evidence_lab.config import load_config, AppConfig
from evidence_lab.api import create_app
from test_api import APIStore, NoInferenceHub

data = load_config('configs/mock.yaml').model_dump(mode='python')
data['runtime'].update(mode='live', embedding_mode='mock', operator_token='')
data['verification'].update(mode='shadow', policy_id=None)
for profile in data['profiles'].values():
    profile['endpoint'] = 'https://provider.test/v1/model'
    profile['api_key'] = SecretStr('test-only-provider-key')
config = AppConfig.model_validate(data)
store = APIStore()
with TestClient(create_app(config, store=store, hub=NoInferenceHub(), initialize=False)) as client:
    response = client.post('/api/queries', json={'question': 'Safe auth boundary reproduction'})
    print({'validated_empty_token': config.runtime.operator_token is not None
           and not bool(config.runtime.operator_token),
           'unauthenticated_status': response.status_code,
           'queued': store.run['id'] == 'new-run'})
PY
```

## Controls verified in the reviewed scope

- A non-empty configured token is compared before all `/api/` handlers. All five
  inference entry points use the shared configuration guard: query, upload,
  evaluation, retry and retrieval preview (`api.py`). Native retry tests confirm
  authenticated transfer from browser ownership to ordinary server workers.
- Browser runtime selection replaces profiles or strips workspace embedding
  credential references; it does not attach server keys to client-selected
  endpoints. Per-request keys are copied into isolated configuration and omitted
  from durable jobs (`runtime_settings.py`, `byok.py`, `storage.py`).
- Provider transport uses explicit ledger reservation, bounded attempts and
  concurrency, disabled redirects and disabled ambient proxies. The SDK uses an
  inert credential and endpoint behind a one-use transport guard
  (`providers/transport.py`, `providers/sdk.py`).
- Status/public runs omit secrets and unreleased answers; provider errors avoid
  raw response bodies. Trace export allowlists workflow metadata rather than
  prompts/keys (`api.py`, `config.py`, `langsmith_trace.py`).
- Vue renders untrusted content through interpolation; no raw HTML sink was
  found in dashboard source. Operator tokens stay in page memory, while browser
  provider keys use localStorage by the user's explicit design
  (`api/client.ts`, `useDashboard.ts`, `useBrowserKeys.ts`).
- Upload admission bounds size/type, normalizes filenames, stores sources through
  parameterized database operations, and serves originals as attachments
  (`api.py`, `ingestion.py`, `storage.py`). This is not a complete PDF parser audit.
- Compose publishes database/API ports on loopback, mounts runtime config
  read-only, and runs the application as a non-root user with no-new-privileges.
  Private YAML/secrets are excluded by `.gitignore` and `.dockerignore`.

## Deployment assumptions and limits

The user confirmed that a fixed operator token is present in deployed config;
the private deployed configuration was not inspected. All holders of that token
have shared operator authority. This review does not assume separate accounts,
tenant isolation or individual revocation.

Network exposure, HTTPS termination, token strength/distribution, config file
permissions, proxy header logging and provider billing limits remain unverified.
HTTP provider endpoints are allowed; arbitrary client endpoints can reach the
API host's network when BYOK calls are allowed. These are conditional deployment
risks, not additional proven server-key theft findings. Restrict egress if BYOK
will be offered to untrusted users on a remotely accessible deployment.

No deployed secrets were read, private data retrieved, real inference performed,
public systems probed, or application data changed. Dependencies were inspected
for relevant transport/validation behavior, not comprehensively scanned for
advisories. Installed versions: FastAPI 0.142.2, Starlette 1.7.0, HTTPX 0.28.1,
Pydantic 2.13.5, pypdf 6.19.0, langchain-openai 1.6.7 and LangSmith 0.14.4.
Current Pydantic field-constraint guidance was checked through Context7;
the empty-token behavior was independently reproduced on the installed version.

## Verification

```sh
EVIDENCE_LAB_TEST_DSN=postgresql://evidence:evidence@127.0.0.1:5432/evidence_security_review_test_20261006 .venv/bin/pytest -q apps/evidence-lab/tests/test_api.py apps/evidence-lab/tests/test_byok.py apps/evidence-lab/tests/test_dashboard_runtime_api.py apps/evidence-lab/tests/test_runtime_settings.py apps/evidence-lab/tests/test_config.py apps/evidence-lab/tests/test_sdk_transport.py apps/evidence-lab/tests/test_upload_body_limit.py
pnpm --filter @evidence-lab/dashboard run test
```

Results: **183 Python tests passed with no skips; 48 dashboard tests passed**.
The dedicated PostgreSQL database was created for this review, private-schema
fixtures provided isolation, and the database was removed afterward. Logs:
`/tmp/evidence-security-review-python.log` and
`/tmp/evidence-security-review-dashboard.log`.

At review time the existing authorization matrix tested `None`, missing/wrong
headers and valid tokens, but not an empty configured token. No product changes
were made during the review; the subsequent authorized remediation adds those
cases as described above.

Remediation verification: `UV_CACHE_DIR=/tmp/evidence-byok-uv task setup` passed.
`EVIDENCE_LAB_TEST_DSN=postgresql://evidence:evidence@127.0.0.1:5432/evidence_empty_token_test_20261006 task check`
passed **507 Python tests and 48 dashboard tests**, with one native backup/restore
prerequisite skip (`EVIDENCE_LAB_TEST_NATIVE_ADMIN_DSN` unset). `git diff --check`
passed. The dedicated database was removed. Logs:
`/tmp/evidence-empty-token-setup.log`, `/tmp/evidence-empty-token-check.log`;
pre-fix regression failures: `/tmp/evidence-empty-token-red.log`.
