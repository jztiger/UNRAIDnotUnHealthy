#!/usr/bin/env bash
# Deploy UNRAIDnotUnHealthy to Unraid via the ghcr.io registry pipeline.
#
# Flow (now that the container is Unraid-template-managed, not compose):
#   1. Verify local working tree is clean
#   2. git push origin main
#   3. Wait for the GitHub Actions build to push the image to ghcr.io
#   4. SSH to Unraid, invoke Unraid's `update_container` PHP script —
#      this is the same code path as clicking Update in the Docker tab:
#      pull image → stop → remove → re-run from template XML → start
#   5. Verify via Grafana's /api/health endpoint
#
# Prerequisites:
#   - gh CLI authenticated (for the workflow watch step)
#   - Unraid host has docker logged into ghcr.io for the image pull
#   - SSH key auth to root@192.168.1.133:2222
#   - Unraid template at /boot/config/plugins/dockerMan/templates-user/my-UNRAIDnotUnHealthy.xml
#
# Configurable via env vars:
#   UNRAID_HOST              192.168.1.133
#   UNRAID_PORT              2222
#   UNRAID_USER              root
#   UNRAID_CONTAINER_NAME    UNRAIDnotUnHealthy
#   UNRAID_WEBUI_HOST        192.168.1.133 (Unraid host IP — Grafana on bridge :3000)
#   UNRAID_WEBUI_PORT        3000
#   SSH_IDENTITY             ~/.ssh/unraid_deploy
#
# Exit codes:
#   0  success
#   1  precondition failed (dirty local tree, push refused, gh CLI missing)
#   2  ssh failure
#   3  remote update_container failed
#   4  health check failed
#   5  CI run not found after push
#   6  CI build failed

set -euo pipefail

UNRAID_HOST="${UNRAID_HOST:-192.168.1.133}"
UNRAID_PORT="${UNRAID_PORT:-2222}"
UNRAID_USER="${UNRAID_USER:-root}"
UNRAID_CONTAINER_NAME="${UNRAID_CONTAINER_NAME:-UNRAIDnotUnHealthy}"
UNRAID_WEBUI_HOST="${UNRAID_WEBUI_HOST:-192.168.1.133}"
UNRAID_WEBUI_PORT="${UNRAID_WEBUI_PORT:-3000}"
SSH_IDENTITY="${SSH_IDENTITY:-$HOME/.ssh/unraid_deploy}"

cd "$(dirname "$0")/.."

log()  { printf '\033[1;34m[deploy]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[deploy] ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[deploy] ⚠\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31m[deploy] ✗\033[0m %s\n' "$*" >&2; }

START_TS=$(date +%s)

# ---------- 1. Local preflight ----------
command -v gh >/dev/null 2>&1 || { fail "gh CLI not installed; required to wait for CI"; exit 1; }

log "checking local working tree…"
if [[ -n "$(git status --porcelain)" ]]; then
  fail "local working tree dirty — commit or stash first"
  echo
  git status --short
  exit 1
fi

LOCAL_COMMIT=$(git rev-parse --short=7 HEAD)
LOCAL_BRANCH=$(git rev-parse --abbrev-ref HEAD)
[[ "$LOCAL_BRANCH" == "main" ]] || warn "local branch is '$LOCAL_BRANCH', not 'main' — remote will pull main"
ok "local clean at $LOCAL_COMMIT ($LOCAL_BRANCH)"

# ---------- 2. Push to origin ----------
log "pushing $LOCAL_BRANCH → origin…"
if ! git push origin "$LOCAL_BRANCH" 2>&1 | sed 's/^/         /'; then
  fail "git push failed"
  exit 1
fi
ok "pushed"

PUSH_SHA=$(git rev-parse HEAD)

# ---------- 3. Wait for CI to build & push image ----------
log "waiting for GitHub Actions run to appear for $PUSH_SHA…"
RUN_ID=""
for i in 1 2 3 4 5 6 7 8 9 10; do
  RUN_ID=$(gh run list --workflow=build.yml --branch=main --limit=5 \
    --json databaseId,headSha --jq ".[] | select(.headSha == \"$PUSH_SHA\") | .databaseId" 2>/dev/null | head -1)
  [[ -n "$RUN_ID" ]] && break
  sleep 3
done
if [[ -z "$RUN_ID" ]]; then
  fail "CI run for $PUSH_SHA not found after 30s"
  exit 5
fi
ok "CI run $RUN_ID started"

log "watching build (typically 1-3 min depending on cache)…"
if ! gh run watch "$RUN_ID" --exit-status >/dev/null 2>&1; then
  fail "CI build failed — see https://github.com/jztiger/UNRAIDnotUnHealthy/actions/runs/$RUN_ID"
  exit 6
fi
ok "image pushed to ghcr.io"

# ---------- 4. SSH to Unraid and trigger native update ----------
SSH_OPTS=("-p" "$UNRAID_PORT" "-o" "StrictHostKeyChecking=accept-new"
          "-o" "BatchMode=yes" "-o" "ConnectTimeout=10")
[[ -f "$SSH_IDENTITY" ]] && SSH_OPTS+=("-i" "$SSH_IDENTITY")

if ! ssh "${SSH_OPTS[@]}" "${UNRAID_USER}@${UNRAID_HOST}" "true" 2>&1; then
  fail "ssh ${UNRAID_USER}@${UNRAID_HOST} -p ${UNRAID_PORT} failed"
  exit 2
fi

log "ssh ${UNRAID_USER}@${UNRAID_HOST} → invoking Unraid update_container…"
ssh "${SSH_OPTS[@]}" "${UNRAID_USER}@${UNRAID_HOST}" \
    "CONTAINER='${UNRAID_CONTAINER_NAME}' bash -s" <<'REMOTE'
set -e

UPDATE_SCRIPT=/usr/local/emhttp/plugins/dynamix.docker.manager/scripts/update_container
[[ -x $UPDATE_SCRIPT || -f $UPDATE_SCRIPT ]] || {
  echo "REMOTE: $UPDATE_SCRIPT not found — is the dynamix.docker.manager plugin installed?" >&2
  exit 3
}

# Pre-deploy revision (best-effort) from container labels
PRE_REV=$(docker inspect "$CONTAINER" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null | cut -c1-7)
[[ -n "$PRE_REV" ]] && echo "REMOTE: image rev before: $PRE_REV"

# This is the same code path as clicking 'Update' in the Docker tab.
# It pulls the image, stops the container, removes it, and re-runs with
# the args generated from the template XML at
# /boot/config/plugins/dockerMan/templates-user/my-UNRAIDnotUnHealthy.xml.
echo "REMOTE: php update_container \"$CONTAINER\""
php "$UPDATE_SCRIPT" "$CONTAINER" || {
  echo "REMOTE: update_container exited non-zero" >&2
  exit 3
}

POST_REV=$(docker inspect "$CONTAINER" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null | cut -c1-7)
[[ -n "$POST_REV" ]] && echo "REMOTE: image rev after:  $POST_REV"
REMOTE
SSH_RC=$?

if [[ $SSH_RC -ne 0 ]]; then
  fail "remote update_container failed (exit $SSH_RC)"
  exit $SSH_RC
fi
ok "Unraid recreated container from template"

# ---------- 5. Verify via Grafana /api/health ----------
log "verifying via http://${UNRAID_WEBUI_HOST}:${UNRAID_WEBUI_PORT}/api/health…"
HEALTHY=""
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
  sleep 3
  RESP=$(curl -sS --max-time 5 "http://${UNRAID_WEBUI_HOST}:${UNRAID_WEBUI_PORT}/api/health" 2>/dev/null) || continue
  if echo "$RESP" | grep -q '"database":[ ]*"ok"'; then
    HEALTHY="$RESP"
    break
  fi
done

if [[ -z "$HEALTHY" ]]; then
  fail "container restarted but Grafana /api/health did not report database ok after ~36s"
  exit 4
fi

GF_VERSION=$(echo "$HEALTHY" | grep -oE '"version":[ ]*"[^"]+"' | head -1)
ok "live: $GF_VERSION"
ELAPSED=$(( $(date +%s) - START_TS ))
ok "deploy finished in ${ELAPSED}s"
