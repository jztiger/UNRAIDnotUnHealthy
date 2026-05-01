#!/usr/bin/env bash
# Build → save → transfer → recreate the unraidnotunhealthy container on a
# remote Unraid host. Use after the first-time manual deploy is done.
#
# Reads config + secrets from scripts/.deploy.env (gitignored). Copy
# scripts/.deploy.env.example to scripts/.deploy.env and fill in your values.
#
# Usage:
#   ./scripts/deploy-to-unraid.sh         # full build + deploy
#   ./scripts/deploy-to-unraid.sh --skip-build   # use existing local image
#   ./scripts/deploy-to-unraid.sh --dry-run      # print plan, don't execute
#
# What it does:
#   1. Builds the image locally via docker-build.sh (unless --skip-build)
#   2. Saves + gzips + ssh-pipes the image to the Unraid Docker daemon
#   3. Stops and removes the existing container (named volumes preserved)
#   4. Recreates with the same env/network/IP/mounts. New volume mounts
#      added in this script are picked up here.
#   5. Verifies the container is running

set -euo pipefail
cd "$(dirname "$0")/.."

# ---------- args ----------
SKIP_BUILD=0
DRY_RUN=0
for a in "$@"; do
  case "$a" in
    --skip-build) SKIP_BUILD=1 ;;
    --dry-run)    DRY_RUN=1 ;;
    -h|--help)    sed -n '2,20p' "$0"; exit 0 ;;
    *)            echo "Unknown arg: $a (use --help)"; exit 2 ;;
  esac
done

# ---------- config ----------
ENV_FILE="scripts/.deploy.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE not found." >&2
  echo "  Copy scripts/.deploy.env.example to $ENV_FILE and fill in your values." >&2
  exit 1
fi
set -a; source "$ENV_FILE"; set +a

: "${UNRAID_HOST:?UNRAID_HOST not set in $ENV_FILE}"
: "${UNRAID_KEY:?UNRAID_KEY not set in $ENV_FILE}"
: "${UNRAID_IP:?UNRAID_IP not set in $ENV_FILE}"
UNRAID_PORT="${UNRAID_PORT:-22}"
UNRAID_USER="${UNRAID_USER:-root}"
CONTAINER="${CONTAINER:-unraidnotunhealthy}"
IMAGE="${IMAGE:-unraidnotunhealthy:dev}"

# ---------- helpers ----------
ssh_run() {
  ssh -i "$UNRAID_KEY" -p "$UNRAID_PORT" \
      -o StrictHostKeyChecking=accept-new \
      -o ConnectTimeout=10 \
      "$UNRAID_USER@$UNRAID_HOST" "$@"
}

note()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[33mWARN:\033[0m %s\n' "$*" >&2; }
fail()  { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

if [ "$DRY_RUN" -eq 1 ]; then
  note "DRY RUN: would deploy $IMAGE to $UNRAID_USER@$UNRAID_HOST:$UNRAID_PORT (container=$CONTAINER ip=$UNRAID_IP)"
  exit 0
fi

# ---------- pre-flight ----------
note "Pre-flight: SSH reachable?"
ssh_run 'true' || fail "Cannot SSH to $UNRAID_USER@$UNRAID_HOST:$UNRAID_PORT — check UNRAID_HOST/PORT/KEY"

note "Pre-flight: container exists on remote?"
if ! ssh_run "docker inspect $CONTAINER >/dev/null 2>&1"; then
  fail "Container '$CONTAINER' not found on remote. First-time deploy must be done manually (see README)."
fi

# ---------- build ----------
if [ "$SKIP_BUILD" -eq 0 ]; then
  note "[1/4] Build image (commit $(git rev-parse --short HEAD))"
  ./scripts/docker-build.sh build
else
  note "[1/4] Skipping build (--skip-build)"
fi

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  fail "Image $IMAGE not present locally. Drop --skip-build."
fi

# ---------- transfer ----------
note "[2/4] Transfer image to $UNRAID_HOST (gzipped pipe over ssh)"
SECONDS=0
docker save "$IMAGE" | gzip -1 | ssh_run "gunzip | docker load" | tail -3
note "    transferred in ${SECONDS}s"

# ---------- recreate ----------
note "[3/4] Stop + remove old container (named volumes preserved)"
ssh_run "docker stop $CONTAINER >/dev/null && docker rm $CONTAINER >/dev/null"

note "[4/4] Recreate with new image"
ssh_run bash -s <<EOF
set -e
docker run -d \\
  --name $CONTAINER \\
  --restart unless-stopped \\
  --privileged \\
  --network eth0 \\
  --ip $UNRAID_IP \\
  -p 3000:3000 \\
  -e TZ=${TZ:-UTC} \\
  -e PUID=${PUID:-1000} -e PGID=${PGID:-1000} \\
  -e PROMETHEUS_RETENTION=${PROMETHEUS_RETENTION:-30d} \\
  -e LOKI_RETENTION=${LOKI_RETENTION:-14d} \\
  -e GF_SECURITY_ADMIN_PASSWORD=${GF_SECURITY_ADMIN_PASSWORD:-admin} \\
  -e SONARR_URL=${SONARR_URL:-http://192.168.1.21:8989} \\
  -e SONARR_API_KEY=${SONARR_API_KEY:-} \\
  -e RADARR_URL=${RADARR_URL:-http://192.168.1.6:7878} \\
  -e RADARR_API_KEY=${RADARR_API_KEY:-} \\
  -v /:/rootfs:ro \\
  -v /proc:/host/proc:ro \\
  -v /sys:/host/sys:ro \\
  -v /sys:/sys:ro \\
  -v /var/lib/docker:/var/lib/docker:ro \\
  -v /var/run/docker.sock:/var/run/docker.sock:ro \\
  -v /var/log:/host/var/log:ro \\
  -v /dev:/dev \\
  -v repo_prometheus-data:/var/lib/prometheus \\
  -v repo_grafana-data:/var/lib/grafana \\
  -v repo_loki-data:/var/lib/loki \\
  -v /mnt/user/plexmedia/dbexport:/var/lib/grafana/plex_data:rw \\
  $IMAGE >/dev/null

# Wait for Grafana to come up
for i in 1 2 3 4 5 6 7 8 9 10; do
  sleep 2
  if curl -sf -o /dev/null -m 2 http://$UNRAID_IP:3000/api/health; then
    echo "Grafana healthy after \${i}x2s"
    break
  fi
done
docker inspect $CONTAINER --format 'Container: {{.Name}} status={{.State.Status}} ip={{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}} image={{.Config.Image}}'
EOF

note "Done. Visit http://$UNRAID_IP:3000"
