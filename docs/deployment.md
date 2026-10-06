# GitHub Actions deployment

Successful `CI` runs for the current `main` commit trigger two deployments:

- **Deploy dashboard** builds the Vue dashboard and uploads it to Cloudflare Pages.
- **Deploy backend** builds the existing Dockerfile, publishes
  `ghcr.io/<owner>/<repository>:sha-<commit>`, and deploys its immutable digest to a VPS.

Pull requests cannot deploy. Both workflows check out the commit that passed CI,
serialize production deployments, and reject commits superseded on `main`.
Run the existing **CI** workflow manually on `main` to redeploy after configuring
credentials. Configure GitHub environment branch restrictions for `main` and any
required reviewers. Environment approval applies before using deployment secrets.

## Cloudflare Pages

Create a **Direct Upload** Pages project with production branch `main`.
Use Wrangler to upload Functions; dashboard drag-and-drop does not deploy them.

Configure the GitHub environment **cloudflare-pages**:

| Kind | Name | Value |
|---|---|---|
| Secret | `CLOUDFLARE_API_TOKEN` | Account-scoped token with Cloudflare Pages Edit permission |
| Variable | `CLOUDFLARE_ACCOUNT_ID` | Cloudflare account ID |
| Variable | `CLOUDFLARE_PAGES_PROJECT` | Existing Direct Upload project name |

In the Pages project's **production runtime variables**, set
`EVIDENCE_LAB_BACKEND_ORIGIN=https://api.example.com`. It must be an HTTPS origin,
without credentials, a path, query, or fragment. Set it before deployment;
redeploy after changing it. No operator token or model key belongs in frontend
build variables or this binding.

The workflow stages assets under `/static/`, matching the existing Vite build,
and serves the entrypoint at `/`. Pages Functions proxy `/api`, `/api/*`,
`/health`, and `/health/*` to the backend. They preserve request bodies and
operator/provider headers, disable response caching, strip cookies, reject
foreign-origin writes, block upstream redirects, and return safe transport errors.
Browser requests remain on the Pages origin; backend CORS changes are unnecessary.
API traffic passes through Cloudflare, including uploaded documents and browser
credentials. Cloudflare Pages Functions usage is separate from static hosting.

## VPS and GHCR

Use an amd64 Linux VPS with Docker Engine, a current Docker Compose supporting
`--wait`, Bash, `flock`, `curl`, and SSH. Give a dedicated deployment user access
to Docker and a deployment directory (default `/opt/evidence-lab`). Docker access
is privileged. For private GHCR packages, log in **on the VPS** as that user using
a separate token with `read:packages` and access to this repository's package.
GitHub Actions uses its own short-lived `GITHUB_TOKEN` to publish with
`packages: write`. An existing package must grant this repository Actions access.

Configure GitHub environment **backend-production**:

| Kind | Name | Value |
|---|---|---|
| Secret | `VPS_HOST` | VPS DNS name or IPv4 address |
| Secret | `VPS_USER` | Dedicated SSH deployment user |
| Secret | `VPS_SSH_KEY` | Private key authorized for that user |
| Secret | `VPS_KNOWN_HOSTS` | Host key verified through a trusted channel; use `[host]:port` for a nonstandard port |
| Variable | `VPS_PORT` | SSH port; defaults to `22` |
| Variable | `VPS_DEPLOY_PATH` | Absolute path without spaces; defaults to `/opt/evidence-lab` |

Provision these private files on the VPS; workflows never upload or overwrite them:

- `.env`: `POSTGRES_PASSWORD=<strong database password>` and
  `FORWARDED_ALLOW_IPS=<trusted proxy addresses as seen inside the API container>`.
  Keep this file readable only by the deployment user. A host reverse proxy
  connecting through Docker often appears as the Compose network gateway.
  Inspect it with `docker network inspect evidence-lab-production_default`.
  For initial provisioning on an isolated VPS where every container and local
  process is trusted, `FORWARDED_ALLOW_IPS=*` can be used; narrow it to the actual
  proxy address once the network exists. The API port stays bound to localhost.
- `runtime.yaml`: full application configuration, based on
  `configs/mock.compose.yaml` for deterministic fixtures or a reviewed live sample.
  Set `database.dsn` to
  `postgresql://evidence:<URL-encoded same password>@db:5432/evidence_lab`.
  Keep the file readable by container UID `10001`, for example owner `10001`
  and mode `600`. The deployment user needs directory access to check its presence.
  Direct private YAML keys work. Environment and secret-file references require
  explicit Compose injection/mounts; this template does not inject them.

Use a configured operator token for public API access. Live inference requires
configured provider keys, limits and budget. Keep policy paths inside
`/app/policies/`; artifacts and policies have persistent volumes. Provision any
required qualified policy separately. Mock mode remains a plumbing fixture.

Provide an HTTPS reverse proxy for `api.example.com` to `127.0.0.1:8000`.
It must preserve the public `Host`, overwrite `X-Forwarded-Proto` with `https`,
and permit the configured upload size and request durations. Uvicorn must trust
its connecting address through `FORWARDED_ALLOW_IPS`; otherwise the existing
origin check rejects POSTs forwarded by Pages. PostgreSQL has no published port.
Protect direct backend access according to the existing operator contract.

The workflow copies deployment files into `releases/<commit>` and runs
`deploy-vps.sh`. The script locks deployments, validates private configuration,
pulls images, waits for PostgreSQL, runs migrations, updates API/worker, and checks
readiness. It records `deployed-image` only after success. Stable Compose project
`evidence-lab-production` preserves PostgreSQL, artifacts, and policies volumes.
It never prunes images or removes volumes. Existing local stacks use different
volumes; importing existing data requires an intentional backup/restore.

Migration failure prevents API/worker replacement. A successful migration followed
by a startup failure may already have changed the database. Take backups before
schema changes and use compatible migrations. There is no automatic rollback or
zero-downtime guarantee; the worker readiness check proves the process is running,
not that it has completed a job. Diagnose failures on the VPS without publishing
private YAML or environment files. Retained release directories permit rerunning
the script with a previous digest only when its schema remains compatible.

## Local checks

```sh
task setup
task check
task build
.venv/bin/python deploy/test_deploy_vps.py
bash -n deploy/deploy-vps.sh
actionlint .github/workflows/*.yml
docker build -t evidence-lab:deployment-check -f apps/evidence-lab/Dockerfile .
EVIDENCE_LAB_IMAGE="ghcr.io/owner/repository@sha256:REPLACE_WITH_64_HEX_DIGEST" \
EVIDENCE_LAB_DEPLOY_ROOT=/opt/evidence-lab \
  docker compose --env-file /opt/evidence-lab/.env -f deploy/compose.vps.yaml config --quiet
```

Use dedicated test DSNs for native tests. See [deployment verification](deployment-verification.md)
for actual checks performed. Credentials, public domains and a prepared VPS/Pages
project are prerequisites; adding these files does not deploy either service.

References: [Pages Direct Upload](https://developers.cloudflare.com/pages/get-started/direct-upload/),
[GHCR authentication](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry),
and [Uvicorn proxy settings](https://www.uvicorn.org/settings/#http).
