## Executive summary

Server keys belong to trusted operator sessions. The deployed configuration has
a fixed operator token (user-confirmed); the API checks that token before every
API route. This review also closed a configuration-dependent gap: live
server-funded inference previously worked if the token was omitted. All five
inference entry points now reject that configuration with 401. A follow-up review
found empty-token configuration also bypassed authentication; remediation now
rejects blank tokens at validation and at the shared inference guard (EL-SEC-001
in `docs/security-review.md`). Browser endpoint
overrides cannot inherit server provider keys. Residual risks center on theft or
sharing of the operator token, trusted configuration changes, and exhaustion of
the configured spending allowance. This is a scoped assessment, not a guarantee
against every form of abuse.

## Scope and assumptions

- Scope: dashboard authentication, runtime selection, provider credentials,
  inference routes, queued execution, budgets and credential disclosure.
- Confirmed: only signed-in operator-token holders may use server keys; the
  deployed config contains the token. The deployed private config was not read.
- Assumption: token holders are trusted operators of this shared workspace,
  not mutually untrusted tenants. There is one shared token, not user accounts.
- Exposure and TLS termination remain unknown. Compose publishes application
  ports on loopback; that does not establish deployed proxy configuration
  (`compose.yaml`, ports; `config.py`, `RuntimeConfig.host`). Transport risks below
  are conditional on access beyond the local host.
- Out of scope: public-system probing, real provider calls, whole-repository
  dependency audit, CI compromise, host/database administrator compromise and
  model-quality claims. Build/dev tools do not establish runtime authorization.
- Open questions: deployed HTTPS termination, secret-file access permissions,
  token distribution/rotation, and provider-side billing caps.

## System model

### Primary components

Vue sends a page-memory operator token as an Authorization bearer header
(`apps/dashboard/src/api/client.ts`, `request`; `composables/useDashboard.ts`,
`connect`, `signOut`). FastAPI authenticates API requests and selects execution
configuration (`api.py`, `operator_boundary`, `browser_config`). PostgreSQL stores
jobs and the persistent call ledger (`storage.py`, `claim_job`, `reserve_call`).
Workers and request-owned background tasks call ProviderHub with server or
request-scoped credentials (`worker.py`, `dispatch`; `api.py`,
`execute_browser_job`). Private YAML/env/files resolve server credentials
(`config.py`, `_resolve_keys`).

### Data flows and trust boundaries

- Browser → API: bearer token, settings, provider keys, questions and uploads over
  HTTP; constant-time token comparison, same-origin write checks, strict request
  schemas and upload bounds. Network encryption depends on deployment.
- Configuration → API/worker: operator-owned YAML and resolved credentials;
  validation and secret redaction, but file access remains a host trust boundary.
- API → PostgreSQL → worker: parameterized durable job inserts and fenced leases;
  browser jobs are reserved atomically and excluded from ordinary worker claims.
  Browser keys are not persisted in job payloads.
- API/worker → provider: configured HTTP endpoints receive credentials and
  evidence. Endpoint syntax is validated; redirects and ambient proxy use are
  disabled. Both HTTP and HTTPS endpoints are permitted by configuration.
- API → dashboard/diagnostics: allowlisted public fields and safe errors; CSP,
  no-store API responses and secret-redacted configuration. Trace routes remain
  operator protected whenever the token is configured.

Evidence: `api.py`, `operator_boundary`, `public_run`, `public_job`;
`runtime_settings.py`, `select_runtime`, `_live_profiles`; `byok.py`,
`request_config`; `providers/transport.py`, `CallExecutor._http`;
`config.py`, `_validate_live_endpoint`, `safe_dict`.

#### Diagram

```mermaid
flowchart LR
  Browser["Browser"] -->|Token and inputs| API["API boundary"]
  Config["Private configuration"] -->|Server credentials| API
  Config -->|Server credentials| Worker["Trusted worker"]
  API -->|Jobs and ledger| DB["PostgreSQL"]
  DB -->|Leased server jobs| Worker
  API -->|Browser owned calls| Provider["External providers"]
  Worker -->|Server owned calls| Provider
  API -->|Redacted responses| Browser
```

## Assets and security objectives

| Asset | Why it matters | Security objective (C/I/A) |
| --- | --- | --- |
| Server provider keys | Authorize paid external calls | C/I |
| Operator token | Grants shared workspace and server-spend authority | C/I |
| Budget ledger and job ownership | Bound cost and select credential owner | I/A |
| Source documents and traces | Contain private evidence and diagnostics | C/I |
| Provider endpoints/configuration | Determine where credentials are sent | I |

## Attacker model

### Capabilities

An attacker with API network access can craft headers, request bodies and uploads,
guess job IDs and attempt token guessing. A browser attacker can control a
separate website. A token thief can perform every operator API operation. A
malicious BYOK client can choose its own endpoint and budget where access permits.

### Non-capabilities

Remote input does not grant private-config, database or process-memory access.
Endpoint selection alone does not copy server keys into browser profiles. A
valid operator token grants full operator authority; separate tenant identities
and corpus permissions are not implemented or assumed.

## Entry points and attack surfaces

| Surface | How reached | Trust boundary | Notes | Evidence |
| --- | --- | --- | --- | --- |
| Query/upload/evaluation | POST API requests | Browser/API/jobs | Can queue paid work | `api.py`: query, upload, start_evaluation |
| Retry | POST job retry | Browser/API/worker | Can transfer a stopped job to server execution only after auth | `api.py`: retry, job_options |
| Retrieval preview | POST preview | Browser/API/provider | Can call embeddings without a durable query | `api.py`: retrieval_preview |
| Mode/settings/key headers | API request headers | Browser/configuration | Isolated browser configuration | `runtime_settings.py`, `byok.py` |
| Status/source/trace routes | GET requests | API/browser | Metadata and evidence; no inference | `api.py`: status, source_version, trace |
| Config/CLI/worker | Local operator processes | Host/provider | Trusted server-key access outside web login | `config.py`, `cli.py`, `worker.py` |

## Top abuse paths

1. **Missing-token deployment:** reach a live server configuration without a token
   → submit query/upload/evaluation/preview/retry → spend server funds. Fixed:
   shared inference configuration guard rejects before side effects.
2. **Stolen/shared token:** obtain the configured bearer token → authenticate
   directly to the API → enqueue server-funded work and read workspace data.
3. **Credential destination manipulation:** submit a malicious endpoint in browser
   settings → try to attach server keys → key inheritance is blocked by profile
   replacement/stripping. Editing private server configuration requires trusted
   host access, but could redirect credentials if that boundary is compromised.
4. **Spending exhaustion:** possess a valid token → submit repeated jobs/previews
   → consume the server's permitted allowance. Atomic ledger limits constrain
   cost, but authorized operators can spend that allowance.
5. **Credential disclosure:** inspect status/errors/traces or induce provider
   failures → seek secrets. Redacted config, public-field filtering and bounded
   generic provider errors reduce this path; host logs and proxy configuration
   remain deployment responsibilities.

## Threat model table

| Threat ID | Threat source | Prerequisites | Threat action | Impact | Impacted assets | Existing controls (evidence) | Gaps | Recommended mitigations | Detection ideas | Likelihood | Impact severity | Priority |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| TM-001 | Unauthenticated API caller | Live server credentials, omitted token, API access | Queue/call paid inference | Unauthorized spend | Server keys/budget | `api.py`: token middleware and new browser_config guard | Former fail-open behavior; now fixed | Retain five-route regression coverage | Count 401 responses without recording headers | Low after fix; high before fix if exposed | High: server-funded access | low |
| TM-002 | Token thief | Token obtained through sharing, config access, or exposed HTTP | Authenticate as operator | Spend allowance/read data | Token, budget, documents | `api.py`: constant-time comparison; dashboard page-memory token | Shared token has no individual revocation; no app login rate limit | Keep config private/untracked; rotate token after exposure; require HTTPS for remote use; proxy token-guess throttling | Alert on unusual authenticated request/spend volume, never log token | Medium if shared or remotely transported | High: full operator scope | high |
| TM-003 | Malicious client or compromised config owner | Client settings or private-config write access | Change credential destination | Key theft | Server keys/endpoints | `runtime_settings.py`: replace/strip credentials; `transport.py`: no redirects | HTTP server endpoints allowed; host config remains trusted | Use HTTPS for server providers; review config changes and restrict file writes | Monitor unexpected outbound destinations | Low for remote clients; host compromise excluded | High if trusted server endpoint changes | low |
| TM-004 | Authorized operator or token thief | Valid token, positive allowance | Repeated inference/jobs | Budget exhaustion/service denial | Budget/availability | `storage.py`: atomic total/phase caps, attempt/concurrency limits | Shared allowance; estimates depend on operator pricing | Set conservative server caps and provider billing limits; do not distribute token to untrusted users | Alert on ledger usage and provider billing discrepancies | Medium: easy once token is held | Medium: bounded allowance and availability | medium |
| TM-005 | API caller or browser code compromise | Access to diagnostics or same-origin execution | Extract keys/token through output/browser state | Credential compromise | Credentials/evidence | `config.py`: safe_dict; `api.py`: CSP, public fields; `transport.py`: safe errors | Deployment logging/TLS not inspected; successful same-origin code compromise can read page token | Preserve output regression tests; exclude Authorization/key headers from proxy logs; retain CSP | Audit secret-free error/log samples | Low on reviewed paths; deployment unknown | High if credentials disclose | low |

## Criticality calibration

- **Critical:** unauthenticated arbitrary server-key extraction or host execution
  permitting unrestricted credential theft. Neither was demonstrated.
- **High:** unauthenticated server-funded inference on an exposed deployment;
  stolen operator token granting full shared-workspace access.
- **Medium:** exhausting a bounded paid allowance with operator authority;
  denying inference by consuming shared concurrency or ledger capacity.
- **Low:** blocked endpoint attempts without server-key inheritance;
  rejected unauthenticated requests; metadata that exposes no credentials.

## Focus paths for security review

| Path | Why it matters | Related Threat IDs |
| --- | --- | --- |
| `apps/evidence-lab/src/evidence_lab/api.py` | Auth and every inference gate | TM-001, TM-002, TM-005 |
| `apps/evidence-lab/src/evidence_lab/runtime_settings.py` | Prevents client endpoints inheriting keys | TM-003 |
| `apps/evidence-lab/src/evidence_lab/byok.py` | Validates and scopes browser secrets | TM-003, TM-005 |
| `apps/evidence-lab/src/evidence_lab/config.py` | Resolves/redacts secrets and validates endpoints | TM-002, TM-003, TM-005 |
| `apps/evidence-lab/src/evidence_lab/providers/transport.py` | Outbound auth, redirects and failures | TM-003, TM-004, TM-005 |
| `apps/evidence-lab/src/evidence_lab/storage.py` | Atomic ownership and paid-call limits | TM-001, TM-004 |
| `apps/dashboard/src/api/client.ts` | Bearer transport and browser-header suppression | TM-002, TM-003 |
| `apps/dashboard/src/composables/useDashboard.ts` | Sign-in validation and token lifecycle | TM-002, TM-005 |

## Notes on use

Scope covers all five discovered inference entry points and the configuration,
queue, provider and response boundaries. Runtime is separated from trusted
CLI/dev/build tooling. User-confirmed token-only authorization is incorporated;
network exposure remains an explicit assumption. No deployment config was read,
real credentials used, or paid call made. Tests verify authorization and plumbing,
not model resistance to malicious evidence.

Verification: `task setup` passed; dedicated PostgreSQL command
`EVIDENCE_LAB_TEST_DSN=postgresql://evidence:evidence@127.0.0.1:5432/evidence_server_keys_test_20261006 task check`
passed **494 Python tests, 48 dashboard tests**, with one native backup/restore
prerequisite skip (`EVIDENCE_LAB_TEST_NATIVE_ADMIN_DSN` unset). Authorization tests
cover missing configuration, absent/wrong bearer tokens, and successful operator
query submission. Existing runtime tests cover server-secret stripping on
browser overrides. Logs: `/tmp/evidence-server-keys-check.log` and
`/tmp/evidence-server-keys-setup.log`. The dedicated database was removed.
