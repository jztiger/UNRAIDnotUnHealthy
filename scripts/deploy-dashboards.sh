#!/usr/bin/env bash
# Fast config-only deploy: push the repo, pull the host checkout, hot-reload.
# No CI, no image rebuild, no container recreate.
#
#   ./scripts/deploy-dashboards.sh                   # dashboards only (hot-reload)
#   ./scripts/deploy-dashboards.sh --restart-grafana # also restart grafana (datasource/provisioning change)
set -euo pipefail

UNRAID_HOST="${UNRAID_HOST:-192.168.1.133}"
UNRAID_PORT="${UNRAID_PORT:-2222}"
UNRAID_USER="${UNRAID_USER:-root}"
WEBUI="${WEBUI:-192.168.1.100:3000}"
SSH_IDENTITY="${SSH_IDENTITY:-$HOME/.ssh/unraid_deploy}"
CONFIG_REPO="${CONFIG_REPO:-/mnt/user/appdb/unraidnotunhealthy/config-repo}"
CONTAINER="${CONTAINER:-UNRAIDnotUnHealthy}"

cd "$(dirname "$0")/.."
ssh_h() { ssh -i "$SSH_IDENTITY" -p "$UNRAID_PORT" "$UNRAID_USER@$UNRAID_HOST" "$@"; }

restart=0
[[ "${1:-}" == "--restart-grafana" ]] && restart=1

echo "[dash] checking working tree…"
[[ -z "$(git status --porcelain)" ]] || { echo "[dash] tree dirty — commit first"; git status --short; exit 1; }
[[ "$(git rev-parse --abbrev-ref HEAD)" == "main" ]] || echo "[dash] WARN: not on main"

echo "[dash] push…"; git push origin main
echo "[dash] pull on host…"
ssh_h "git -C '$CONFIG_REPO' fetch --quiet origin main && git -C '$CONFIG_REPO' reset --hard --quiet origin/main && git -C '$CONFIG_REPO' rev-parse --short HEAD"

if [[ "$restart" == 1 ]]; then
  echo "[dash] restarting grafana (provisioning/datasource change)…"
  ssh_h "docker exec $CONTAINER /command/s6-svc -r /run/service/grafana"
fi

echo "[dash] verifying health…"
for i in $(seq 1 12); do
  if ssh_h "curl -s -m3 http://$WEBUI/api/health" | grep -q '"database": "ok"'; then echo "[dash] ✓ grafana healthy"; break; fi
  sleep 5
done
echo "[dash] done. Dashboard JSON hot-reloads within ~30s; provisioning needs the restart flag."
