# GitHub Actions deployment

See [CI](ci.md) for the native test/no-skip gate and local reproduction commands.
This guide covers the deployment workflows and shared VPS proxy. Both targets
were verified in mock mode on 2026-10-06; this does not establish live inference
quality or production readiness.

| Service | Current address |
|---|---|
| Pages dashboard | <https://evidence-lab-16x.pages.dev> |
| VPS API and same-origin dashboard | <https://evidence-lab.blublux.com> |

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

The checked-in [Wrangler configuration](../apps/dashboard/wrangler.jsonc) defines
the Pages output directory and compatibility date. Wrangler is pinned in the
dashboard's development dependencies and installed by `task setup`.
CI uses `task dashboard:pages:build` and `task dashboard:pages:deploy`.
The deploy task fills the backend variable with a single `jq` assignment and runs
native `wrangler pages deploy`. The Direct Upload project must use production
branch `main`. Wrangler 4.147.0 can create projects interactively, but a missing
project fails in noninteractive CI. Create it once with the native command:

```sh
pnpm --filter @evidence-lab/dashboard exec wrangler pages project create "$CLOUDFLARE_PAGES_PROJECT" --production-branch main
```

Configure the GitHub environment **cloudflare-pages**:

| Kind | Name | Value |
|---|---|---|
| Secret | `CLOUDFLARE_API_TOKEN` | Account-scoped token with Cloudflare Pages Edit permission |
| Variable | `CLOUDFLARE_ACCOUNT_ID` | Cloudflare account ID |
| Variable | `CLOUDFLARE_PAGES_PROJECT` | Existing Direct Upload project name |
| Variable | `EVIDENCE_LAB_BACKEND_ORIGIN` | `https://evidence-lab.blublux.com` |

Wrangler reads the account ID and API token from its supported environment
variables; `--project-name` selects the GitHub-configured project and overrides
the config's default name. Before deployment, `jq` fills
`vars.EVIDENCE_LAB_BACKEND_ORIGIN` in `wrangler.jsonc` from the GitHub variable.
Wrangler deploys it as a plain runtime variable. The placeholder is replaced
explicitly because Wrangler does not expand `${...}`. This changes the local
working copy of the config when running the deploy task outside CI.
The origin must be an HTTPS origin,
without credentials, a path, query, or fragment, and must differ from the Pages
dashboard origin. No Cloudflare dashboard variable edits are needed;
rerun CI after changing the GitHub variable. No operator token or model key belongs in frontend
build variables or this binding.

For a local deployment, run `task dashboard:pages:build`, then
`task dashboard:pages:deploy` with the same four environment values plus
`DEPLOY_SHA` set to the full tested commit SHA. The deploy task requires `jq`.
The GitHub workflow provides these inputs and deploys only after successful CI.

The workflow stages assets under `/static/`, matching the existing Vite build,
and serves the entrypoint at `/`. Pages Functions proxy `/api`, `/api/*`,
`/health`, and `/health/*` to the backend. They preserve request bodies and
operator/provider headers, disable response caching, strip cookies, reject
foreign-origin writes, block upstream redirects, and return safe transport errors.
Browser requests remain on the Pages origin; backend CORS changes are unnecessary.
API traffic passes through Cloudflare, including uploaded documents and browser
credentials. Cloudflare Pages Functions usage is separate from static hosting.
BYOK keys/settings are saved per browser origin; users must re-enter them when
moving from localhost to the deployed dashboard domain. Operator tokens remain in
page memory and are entered through **Sign in**, never frontend variables. See
[BYOK](byok.md) for credential lifetime and server-vs-browser behavior.

Use the stable Pages address above for normal access. Hash-prefixed deployment
URLs retain their deployed configuration; they do not pick up later variable
fixes. An older deployment used `evidence-lab.blueblux.com` (an extra `e`) and
returned HTTP 530 with Cloudflare error 1016. The correct domain is
`evidence-lab.blublux.com`. After changing the GitHub variable, redeploy and open
the stable address. A JSON 401 stating that an operator token is required means
the proxy reached the API; enter the token through **Sign in**. Sign-in state is
separate on the Pages and backend origins.

Files are shared by all users with workspace access. A shared operator token
does not establish individual identities or workspace isolation. The dashboard
displays this notice; [isolation](todo.md) remains planned.

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
| Variable | `VPS_DEPLOY_PATH` | Absolute path using letters, digits, `_`, `-` and `/`; cannot be `/`; defaults to `/opt/evidence-lab` |

Provision these private files on the VPS; workflows never upload or overwrite them:

- `.env`: `POSTGRES_PASSWORD=<strong database password>` and
  `FORWARDED_ALLOW_IPS=<trusted proxy addresses as seen inside the API container>`.
  Keep this file readable only by the deployment user. A host reverse proxy
  connecting through Docker often appears as the Compose network gateway.
  Inspect it with `docker network inspect evidence-lab-production_default`.
  Trust the actual gateway address rather than `*`. The API port stays bound to
  localhost.
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

### Shared host Caddy

The VPS uses one host-level Caddy systemd service shared by all projects. Its
`/etc/caddy/Caddyfile` imports `/etc/caddy/sites-enabled/*.caddy`; each project
has a separate root-owned configuration in `/etc/caddy/sites-available/`, enabled
with a symlink. Only host Caddy binds public ports 80/443. Project containers
publish backend ports on localhost; do not run a competing project proxy.

The checked-in `deploy/caddy/evidence-lab.caddy` maps
`evidence-lab.blublux.com` to `127.0.0.1:8000`. Install it as an administrator:

```sh
sudo install -m 644 deploy/caddy/evidence-lab.caddy /etc/caddy/sites-available/evidence-lab.caddy
sudo ln -sfn /etc/caddy/sites-available/evidence-lab.caddy /etc/caddy/sites-enabled/evidence-lab.caddy
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl reload caddy
```

Install the official stable Caddy package and create the two site directories
once per host. `deploy/caddy/Caddyfile` is the shared entrypoint; application CI
does not overwrite shared host configuration or restart other projects. DNS must
route the hostname to the VPS; use Full (strict) TLS if Cloudflare proxies it.
Set the Pages environment variable `EVIDENCE_LAB_BACKEND_ORIGIN` to
`https://evidence-lab.blublux.com`.

Caddy preserves the public `Host` and sets `X-Forwarded-Proto` for HTTPS.
It permits the application's upload sizes and request durations. Uvicorn must trust
its connecting address through `FORWARDED_ALLOW_IPS`; otherwise the existing
origin check rejects POSTs forwarded by Pages. PostgreSQL has no published port.
Protect direct backend access according to the existing operator contract.

Sales Coach has a separate `deploy/caddy/salescoach.caddy` site. Its VPS
`/opt/salescoach-staging/config/docker-compose.public-dev.yml` overlay publishes
API `127.0.0.1:8001`, dashboard `127.0.0.1:3001`, and object storage
`127.0.0.1:9001`, and excludes the old Caddy service with a profile. Include that
overlay when resuming or deploying Sales Coach; its source CI must retain those
settings. Sales Coach remains deliberately stopped, with containers and volumes
preserved. Its proxy returns HTTP 503 until the backends resume. Migration notes
are in `/opt/salescoach-staging/config/HOST-CADDY.md` on the VPS.

See [deployment verification](ci.md#deployment-verification-2026-10-06) for the
observed checks and their limits.

### Remote MCP

MCP is mounted at `https://evidence-lab.blublux.com/api/mcp/`. Configure a nonempty private
`runtime.operator_token` and add the actual backend hostname to
`agent_rag.allowed_hosts`. Declare permitted corpora in `allowed_corpora` and
applicable browser origins in `allowed_origins`; defaults allow loopback hosts
only. Preserve `Host` and correct proxy scheme handling. No provider keys or BYOK
overrides belong in MCP tool calls.

Use the direct backend URL for agents. The Pages Function forwards `/api/*`, but
its same-origin write check can reject browser clients before the MCP SDK Origin
allowlist runs. MCP does not configure browser CORS. Server/CLI clients without
browser Origin headers still require bearer authentication and an allowed Host.
See [MCP setup](agent-rag-interface.md) for a client example and request/result caps.
The token also grants administrative REST access; this interface is for trusted
operators, with scoped OAuth delegation deferred. Enforce any deployment rate
limits at the existing proxy; per-client application quotas are not implemented.

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

Use dedicated test DSNs for native tests. See [CI](ci.md) for the shared check gate.
Credentials, public domains and a prepared VPS/Pages project are prerequisites
for a new installation. Application deployments do not install or update the
shared host proxy.

References: [Pages Direct Upload](https://developers.cloudflare.com/pages/get-started/direct-upload/),
[Wrangler Pages configuration](https://developers.cloudflare.com/pages/functions/wrangler-configuration/),
[GHCR authentication](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry),
[Uvicorn proxy settings](https://www.uvicorn.org/settings/#http),
and [Caddy service management](https://caddyserver.com/docs/running#using-the-service).
