#!/usr/bin/env bash
# Invoked on the VPS; credentials and application configuration stay on that host.
set -euo pipefail
umask 077

if [[ $# != 2 ]]; then
  echo 'Usage: deploy-vps.sh ghcr.io/owner/image@sha256:digest /absolute/deploy/path' >&2
  exit 2
fi
image=$1
deploy_root=$2
if [[ ! "$image" =~ ^ghcr\.io/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$ ]]; then
  echo 'An immutable GHCR sha256 image reference is required.' >&2
  exit 2
fi
if [[ ! "$deploy_root" =~ ^/[a-zA-Z0-9_/-]+$ || "$deploy_root" == / ]]; then
  echo 'An absolute deployment path with safe characters is required.' >&2
  exit 2
fi
for private_file in .env runtime.yaml; do
  if [[ ! -f "$deploy_root/$private_file" ]]; then
    echo "Missing private VPS file: $private_file" >&2
    exit 2
  fi
done

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export EVIDENCE_LAB_IMAGE="$image"
export EVIDENCE_LAB_DEPLOY_ROOT="$deploy_root"
# Serialize manual runs as well as CI; a failed run leaves the prior release record.
exec 9>"$deploy_root/deploy.lock"
flock -w 600 9
compose=(docker compose --project-name evidence-lab-production --env-file "$deploy_root/.env" -f "$script_dir/compose.vps.yaml")
"${compose[@]}" config --quiet
"${compose[@]}" pull db migrate api worker
"${compose[@]}" up --detach --wait --wait-timeout 120 db
# No API/worker replacement if migrations fail. Existing volumes are never deleted.
"${compose[@]}" run --rm --no-deps migrate
"${compose[@]}" up --detach --no-deps --wait --wait-timeout 180 api worker
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:8000/health/ready >/dev/null
printf '%s\n' "$image" >"$deploy_root/deployed-image.tmp"
mv -- "$deploy_root/deployed-image.tmp" "$deploy_root/deployed-image"
echo 'Backend API is ready and worker is running.'
