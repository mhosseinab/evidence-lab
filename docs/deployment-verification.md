# Deployment engineering verification

Verified on 2026-10-06. This covers local engineering behavior, not live cloud
deployment, model quality, endpoint performance, or production readiness.

| Check | Actual result |
|---|---|
| `UV_CACHE_DIR=/tmp/evidence-lab-ci-uv-cache task setup` | Passed; locked dependencies installed |
| `task check` without native DSNs | Passed: 463 backend tests passed, 45 prerequisite skips; original 48 dashboard tests passed |
| Final `task check` with dedicated native test/admin DSNs | Passed: 508 backend tests, 56 dashboard tests; no skips |
| `.venv/bin/python deploy/test_deploy_vps.py` | Passed: 5 tests for mutable image rejection, missing private config, migration failure, readiness failure and update order |
| `UV_CACHE_DIR=/tmp/evidence-lab-ci-uv-cache task build` | Passed: dashboard, wheel and source distribution |
| `actionlint .github/workflows/*.yml` (1.7.7) | Passed for CI and both deployment workflows |
| `bash -n deploy/deploy-vps.sh` | Passed |
| `.venv/bin/ruff check deploy/test_deploy_vps.py` | Passed |
| `docker compose ... config --quiet` | Passed with test image, private fixture env/config and isolated port override |
| `DOCKER_CONFIG=/tmp/evidence-ci-docker-config docker build -t evidence-lab:ci-deployment-check -f apps/evidence-lab/Dockerfile .` | Passed |
| Wrangler 4.147.0 `pages functions build --outdir=/tmp/evidence-pages-worker` from `apps/dashboard` | Passed; compiled Worker |
| Isolated VPS Compose smoke | Migrations passed; API/worker started; `/`, favicon, readiness and status returned 200; HTTPS proxy-header POST returned 201; mock seed ingestion completed |
| `git diff --check` | Passed |

Initial setup/build attempts could not write the sandbox's default uv/Docker
cache directories; temporary cache/config paths resolved this. An intermediate
native check stopped on frontend formatting while the new proxy file was being
edited; formatting was corrected before the final complete check.

## Reproduce native checks

The verification database was created specifically for this task, separate from
all running application databases. The existing private-schema fixtures and
native restore tests managed their own disposable schemas/databases.

```sh
docker run -d --name evidence-ci-check-20261006 \
  -p 127.0.0.1::5432 \
  -e POSTGRES_DB=evidence_ci_test -e POSTGRES_USER=evidence \
  -e POSTGRES_PASSWORD=evidence \
  pgvector/pgvector:0.8.2-pg17-bookworm@sha256:feb68f4f15446397d8cac7f4fe48fe4586de83160d1fc48b46283312d1a33966
docker exec evidence-ci-check-20261006 pg_isready -U evidence -d evidence_ci_test
docker port evidence-ci-check-20261006 5432/tcp
# Substitute the reported localhost port (32768 in this run).
EVIDENCE_LAB_TEST_DSN=postgresql://evidence:evidence@127.0.0.1:32768/evidence_ci_test \
EVIDENCE_LAB_TEST_NATIVE_ADMIN_DSN=postgresql://evidence:evidence@127.0.0.1:32768/evidence_ci_test \
  task check
```

Native tests used PostgreSQL 17 with pgvector and PostgreSQL 18.6 dump/restore
clients. CI installs PostgreSQL 17 clients to match its server.

The separate Compose smoke used project `evidence-ci-vps-20261006`, fixture
configuration copied from `configs/mock.compose.yaml`, the locally built image,
and an ephemeral localhost API port. It did not execute the SSH workflow or pull
a private GHCR image. Successful HTTPS-origin POST validation used the Docker
gateway in `FORWARDED_ALLOW_IPS` and public `Host`/`Origin` plus
`X-Forwarded-Proto: https`. No provider inference calls were made.

Test containers were removed and the smoke network was stopped after verification;
test volumes and the local test image were retained. Existing deployments,
application volumes and dependencies were preserved. Logs are in `/tmp`, not
committed documentation.

## External prerequisites not exercised

GitHub hosted workflow execution, GHCR publication/private pulls, SSH host-key
validation against the real VPS, actual Pages upload, DNS/TLS and the public
reverse proxy remain unverified until credentials and infrastructure are set up.
The [deployment guide](deployment.md) lists the required settings. Local fixture
checks do not measure human-reviewed held-out gold or real endpoint performance.

## Local `task ci` follow-up

On 2026-10-06, the shared local/GitHub CI entrypoint passed with an isolated
PostgreSQL/pgvector container named `evidence-local-ci-20261006`:

```sh
UV_CACHE_DIR=/tmp/evidence-lab-ci-uv-cache \
EVIDENCE_LAB_TEST_DSN=postgresql://evidence:evidence@127.0.0.1:32771/evidence_ci_test \
EVIDENCE_LAB_TEST_NATIVE_ADMIN_DSN=postgresql://evidence:evidence@127.0.0.1:32771/evidence_ci_test \
  task ci
```

Locked setup, dependency consistency, lint/type checks, 518 backend tests,
56 dashboard tests, 5 deployment-script tests, the no-skip JUnit gate and both
builds passed. Counts reflect the current workspace, including concurrent backend
changes outside this task. Actionlint and `git diff --check` also passed.
Running `task ci` without the DSNs failed at the intended prerequisite check
before setup or tests. The dedicated test container was removed afterward; no
application database was used. The full run log is `/tmp/evidence-local-ci.log`.
