# CI checks and release flow

The source of truth is [ci.yml](../.github/workflows/ci.yml) and the root
[Taskfile](../Taskfile.yml). CI runs for pull requests, pushes to `main` and manual
dispatch. The check job uses an isolated PostgreSQL 17/pgvector service and
matching PostgreSQL clients; it never needs real model keys or paid calls.

`task ci` performs locked Python/frontend setup, lock/dependency consistency
checks, lint and type checks, native Python tests, dashboard tests,
deployment-script tests, a no-skip backend JUnit gate, and dashboard/wheel/source
builds. Missing native prerequisites or skipped backend tests fail the gate.
GitHub uploads the JUnit report and build outputs with seven-day retention, even
after a failed job when outputs exist. New CI runs cancel superseded runs for the
same ref; production deployment workflows serialize instead.

## Reproduce locally

From the repository root, install the development prerequisites listed in the
[README](../README.md#local-development), matching `pg_dump`/`pg_restore` clients,
and prepare a dedicated PostgreSQL/pgvector test database. Never select an
application database. Supply both DSNs explicitly:

```sh
EVIDENCE_LAB_TEST_DSN=postgresql://evidence:evidence@127.0.0.1:5432/evidence_test \
EVIDENCE_LAB_TEST_NATIVE_ADMIN_DSN=postgresql://evidence:evidence@127.0.0.1:5432/evidence_test \
  task ci
```

The admin connection needs permission to create/drop the uniquely named disposable
databases used by native restore tests. Other native tests use private schemas.
`task check` can report prerequisite skips; it does not provide CI's no-skip gate.
Use `task test:offline` for non-database checks. Local `task ci` builds artifacts
but does not publish images, upload Pages assets or deploy a VPS.

## Deployment after CI

Successful CI for the current `main` commit triggers:

- [Deploy dashboard](../.github/workflows/deploy-dashboard.yml): rebuilds the
  locked dashboard, stages `/static/` assets and deploys Pages Functions/assets.
- [Deploy backend](../.github/workflows/deploy-backend.yml): builds an amd64 image,
  publishes its commit tag to GHCR, then deploys the immutable digest over verified
  SSH. The VPS script migrates the database before replacing API/worker.

Pull requests cannot deploy. Workflows check out the tested commit and check that
it is still current on `main`; backend deployment checks again after environment
approval. Configure the `cloudflare-pages` and `backend-production` environments,
their secrets/variables and branch restrictions as described in
[deployment setup](deployment.md). Run CI manually on `main` to trigger deployment
after provisioning. The two targets deploy independently; there is no atomic
frontend/backend rollout or automatic database rollback.

Local fixture checks do not establish hosted workflow success, live endpoint
performance or production readiness. Keep execution logs locally or as CI
artifacts; Git records source and validation history.

## Deployment verification (2026-10-06)

The locked Wrangler 4.147.0 configuration and native Pages command compiled and
uploaded the Functions bundle successfully. The dashboard deployment workflow
[run 37431700794](https://github.com/mhosseinab/evidence-lab/actions/runs/37431700794)
completed successfully, including a rerun after investigating an old deployment
URL. The current stable address is <https://evidence-lab-16x.pages.dev>.

Live checks confirmed:

- Pages and VPS readiness endpoints returned HTTP 200 and mock mode.
- Pages returned JSON 401 without the operator token and HTTP 200 for authenticated
  `/api/corpora` reads.
- Authenticated empty query requests reached validation (422) through both Pages
  and the direct backend. Foreign-origin writes returned 403. No records or paid
  inference calls were created by these checks.
- Host Caddy 2.11.7 is active and enabled at boot. Configuration validation and
  graceful reload passed. Caddy owns ports 80/443; its admin port 2019 and the API
  port 8000 are bound to localhost.
- The Sales Coach Compose overlay validated with localhost ports and its old
  Caddy service excluded. Sales Coach stays stopped with its data preserved; its
  HTTPS route returns 503.

Recheck the unauthenticated endpoints without exposing credentials:

```sh
curl --fail https://evidence-lab-16x.pages.dev/health/ready
curl --fail https://evidence-lab.blublux.com/health/ready
curl -i https://evidence-lab-16x.pages.dev/api/corpora
```

On the VPS, validate the proxy before reloading it:

```sh
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl is-active caddy
sudo systemctl is-enabled caddy
```

The earlier full `task ci` passed against a dedicated PostgreSQL/pgvector container:
542 backend tests, 62 dashboard tests, 5 deployment-script tests, no backend skips,
and successful dashboard/wheel/source builds. The test container was removed
following that run; its ephemeral port is not a reusable prerequisite. Use the
[local reproduction instructions](#reproduce-locally) with a dedicated database.
The original log is `/tmp/evidence-pages-native-ci.log` on the development host.

For the host-proxy change, `task check` passed with 495 backend tests and 62
dashboard tests; 47 native PostgreSQL tests were skipped because dedicated test
DSNs were unset. `.venv/bin/python deploy/test_deploy_vps.py` passed all five tests,
and `git diff --check` passed. The check log is
`/tmp/evidence-host-caddy-check.log` on the development host. The follow-up is not
claimed as a complete native run. Live model quality, MCP hostname permissions,
and Sales Coach backend readiness were not qualified by this deployment check.
