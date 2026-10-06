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

## Wrangler configuration verification (2026-10-06)

The Pages workflow now uses the checked-in `apps/dashboard/wrangler.jsonc` and
the native Wrangler deployment command. No custom provisioning
script remains; `jq` fills the plain backend-origin variable from GitHub before
deployment. `wrangler pages functions build` with locked
Wrangler 4.147.0 compiled the Worker successfully using the checked-in config.
`task dashboard:pages:build`, a dry run of `task dashboard:pages:deploy`,
Actionlint, Biome and `git diff --check` passed.

The full `task ci` passed against a dedicated PostgreSQL/pgvector container:
542 backend tests, 62 dashboard tests, 5 deployment-script tests, no skipped
backend tests, and successful dashboard/wheel/source builds. The runnable command
was:

```sh
UV_CACHE_DIR=/tmp/evidence-lab-ci-uv-cache \
EVIDENCE_LAB_TEST_DSN=postgresql://evidence:evidence@127.0.0.1:32775/evidence_ci_test \
EVIDENCE_LAB_TEST_NATIVE_ADMIN_DSN=postgresql://evidence:evidence@127.0.0.1:32775/evidence_ci_test \
  task ci
```

The task-created container `evidence-wrangler-ci-20261006` was removed after the
run. Native tests used their existing isolated schema/database fixtures; no
application data was used. Logs are in `/tmp/evidence-pages-native-ci.log` and
`/tmp/evidence-pages-native-wrangler.log`. Initial dependency installation required
explicit pnpm build approval for esbuild/workerd; the approved native build tools
then installed successfully. No authenticated Cloudflare command or deployment
was executed; live provisioning and runtime variable updates remain unverified.

The subsequent plain-variable change was verified with `jq` and Wrangler's
installed config parser: the GitHub-provided URL resolves as a plain `vars`
binding. Actionlint, Biome and `git diff --check` passed. `task check` passed with
495 backend tests and 62 dashboard tests, with 47 backend prerequisite skips.
Native backend prerequisites were not supplied for this follow-up; the skips are recorded in
`/tmp/evidence-pages-vars-check.log` rather than claimed as a complete native run.
