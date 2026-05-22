# Grafana Config Bind-Mount Migration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Serve Grafana dashboards + provisioning from a host git checkout via read-only bind-mounts, so config changes deploy with `git pull` + hot-reload (seconds, no image rebuild, no recreate) instead of the full CI pipeline.

**Architecture:** The image keeps baking dashboards/provisioning as a fallback, but two read-only bind-mounts (`/etc/grafana/dashboards`, `/etc/grafana/provisioning`) shadow them with content from a host clone at `/mnt/user/appdb/unraidnotunhealthy/config-repo`. A new `deploy-dashboards.sh` pushes the repo and pulls on the host; Grafana's provider (`updateIntervalSeconds: 30`) hot-reloads dashboard JSON. Datasource/provisioning YAML changes apply via `s6-svc -r grafana` (no recreate).

**Tech Stack:** Grafana provisioning, Unraid Docker template XML, git, Bash, SSH.

**Spec:** `docs/superpowers/specs/2026-05-21-grafana-config-bindmount-design.md`

**Key facts (verified this session):**
- Unraid SSH: `ssh -i ~/.ssh/unraid_deploy -p 2222 root@192.168.1.133`
- Grafana runs on macvlan **192.168.1.100:3000**; admin/admin. Health: `curl http://192.168.1.100:3000/api/health` → `database: ok`.
- Server-side rendering works (verify changes by PNG): `curl -u admin:admin -o out.png 'http://192.168.1.100:3000/render/d-solo/plex-media-analysis/plex-media-analysis?panelId=15&var-library=2&width=800&height=400&from=now-6h&to=now'`
- Repo is **public**; host pulls over anonymous HTTPS.
- `UNRAIDnotUnHealthy` repo auto-commits **and pushes** after every change.
- The repo `unraid-template.xml` and the live `/boot/.../my-UNRAIDnotUnHealthy.xml` have **diverged** — edit both, separately.
- This is infra-ops, not unit-tested code: "tests" are verification commands + rendered PNGs.

**Ordering rationale:** Make + push all repo changes FIRST (so the host clone has them), then clone on the host, then edit the live template, then one recreate to apply the mounts, then prove the hot-reload loop.

---

## Task 1: Set provisioning read-only (file-first, no UI drift)

**Files:**
- Modify: `grafana/provisioning/dashboards/default.yml:9`

- [ ] **Step 1: Flip allowUiUpdates to false**

Change line 9 of `grafana/provisioning/dashboards/default.yml`:
```yaml
    allowUiUpdates: false
```
(was `true`). This makes git the source of truth — the Grafana UI cannot overwrite provisioned dashboards.

- [ ] **Step 2: Commit + push**

```bash
cd /home/jztiger/UNRAIDnotUnHealthy
git add grafana/provisioning/dashboards/default.yml
git commit -m "Grafana provisioning read-only (allowUiUpdates: false) for file-first dashboards"
git push
```

---

## Task 2: Fix deploy-unraid.sh health-check IP

**Files:**
- Modify: `scripts/deploy-unraid.sh:43`

- [ ] **Step 1: Point the default WebUI host at the macvlan IP**

Change line 43 of `scripts/deploy-unraid.sh`:
```bash
UNRAID_WEBUI_HOST="${UNRAID_WEBUI_HOST:-192.168.1.100}"
```
(was `192.168.1.133`). Grafana listens on the macvlan IP `.100`, not the management IP `.133`, so the post-deploy `/api/health` check stops false-failing. Also update the comment on line 24 to read `192.168.1.100`.

- [ ] **Step 2: Commit + push**

```bash
cd /home/jztiger/UNRAIDnotUnHealthy
git add scripts/deploy-unraid.sh
git commit -m "Fix deploy-unraid.sh health check: Grafana is on macvlan .100, not .133"
git push
```

---

## Task 3: Create deploy-dashboards.sh

**Files:**
- Create: `scripts/deploy-dashboards.sh`

- [ ] **Step 1: Write the script**

Create `scripts/deploy-dashboards.sh`:
```bash
#!/usr/bin/env bash
# Fast config-only deploy: push the repo, pull the host checkout, hot-reload.
# No CI, no image rebuild, no container recreate.
#
#   ./scripts/deploy-dashboards.sh                  # dashboards only (hot-reload)
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
```

- [ ] **Step 2: Make executable + commit + push**

```bash
cd /home/jztiger/UNRAIDnotUnHealthy
chmod +x scripts/deploy-dashboards.sh
git add scripts/deploy-dashboards.sh
git commit -m "Add deploy-dashboards.sh: fast config-only deploy (push + host pull + hot-reload)"
git push
```

---

## Task 4: Add bind-mounts to the repo template

**Files:**
- Modify: `unraid-template.xml`

- [ ] **Step 1: Add two read-only Path Config entries**

In `unraid-template.xml`, after the `Grafana Data` `<Config>` entry (the one with `Target="/var/lib/grafana"`), add:
```xml
  <Config Name="Grafana Dashboards (git)" Target="/etc/grafana/dashboards" Default="/mnt/user/appdb/unraidnotunhealthy/config-repo/grafana/dashboards" Mode="ro" Description="Dashboards served from a host git checkout (read-only) so changes deploy via git pull + hot-reload, no rebuild. Leave default unless you relocate the config-repo clone." Type="Path" Display="advanced" Required="false" Mask="false">/mnt/user/appdb/unraidnotunhealthy/config-repo/grafana/dashboards</Config>
  <Config Name="Grafana Provisioning (git)" Target="/etc/grafana/provisioning" Default="/mnt/user/appdb/unraidnotunhealthy/config-repo/grafana/provisioning" Mode="ro" Description="Datasource + dashboard-provider YAML from the host git checkout (read-only). Datasource changes need a grafana restart (s6-svc -r), not a recreate." Type="Path" Display="advanced" Required="false" Mask="false">/mnt/user/appdb/unraidnotunhealthy/config-repo/grafana/provisioning</Config>
```

Also add matching `<Volume>` mappings inside the `<Data>` block if the template uses one (mirror how `/var/lib/grafana` appears there):
```xml
      <Volume>
        <HostDir>/mnt/user/appdb/unraidnotunhealthy/config-repo/grafana/dashboards</HostDir>
        <ContainerDir>/etc/grafana/dashboards</ContainerDir>
        <Mode>ro</Mode>
      </Volume>
      <Volume>
        <HostDir>/mnt/user/appdb/unraidnotunhealthy/config-repo/grafana/provisioning</HostDir>
        <ContainerDir>/etc/grafana/provisioning</ContainerDir>
        <Mode>ro</Mode>
      </Volume>
```

- [ ] **Step 2: Validate XML + commit + push**

```bash
cd /home/jztiger/UNRAIDnotUnHealthy
python3 -c "import xml.dom.minidom; xml.dom.minidom.parse('unraid-template.xml'); print('valid XML')"
git add unraid-template.xml
git commit -m "Template: add read-only bind-mounts for git-served dashboards + provisioning"
git push
```
Expected: `valid XML`.

---

## Task 5: Clone the config repo on the host

**Files:** none (host action)

- [ ] **Step 1: Clone the repo to the host config path**

```bash
ssh -i ~/.ssh/unraid_deploy -p 2222 root@192.168.1.133 \
  "git clone https://github.com/jztiger/UNRAIDnotUnHealthy.git /mnt/user/appdb/unraidnotunhealthy/config-repo 2>&1 | tail -3"
```
Expected: clone succeeds (anonymous HTTPS, public repo).

- [ ] **Step 2: Verify the mount source dirs exist + match the repo**

```bash
ssh -i ~/.ssh/unraid_deploy -p 2222 root@192.168.1.133 \
  "git -C /mnt/user/appdb/unraidnotunhealthy/config-repo rev-parse --short HEAD; \
   ls /mnt/user/appdb/unraidnotunhealthy/config-repo/grafana/dashboards/plex-media.json; \
   ls /mnt/user/appdb/unraidnotunhealthy/config-repo/grafana/provisioning/datasources/datasources.yml"
```
Expected: the HEAD short SHA matches the latest pushed commit; both files listed.

---

## Task 6: Add bind-mounts to the LIVE template (with backup)

**Files:** none (edits `/boot/.../my-UNRAIDnotUnHealthy.xml` on the host)

The live template drives `update_container` on recreate. It has diverged from the repo template, so edit it directly — carefully, with a backup.

- [ ] **Step 1: Back up the live template**

```bash
ssh -i ~/.ssh/unraid_deploy -p 2222 root@192.168.1.133 \
  "cp /boot/config/plugins/dockerMan/templates-user/my-UNRAIDnotUnHealthy.xml \
      /boot/config/plugins/dockerMan/templates-user/my-UNRAIDnotUnHealthy.xml.bak-bindmount"
```

- [ ] **Step 2: Insert the two `<Config>` entries after the Grafana Data entry**

Pull the live template locally, edit, push back (safer than in-place sed):
```bash
scp -i ~/.ssh/unraid_deploy -P 2222 \
  root@192.168.1.133:/boot/config/plugins/dockerMan/templates-user/my-UNRAIDnotUnHealthy.xml /tmp/live-template.xml
```
In `/tmp/live-template.xml`, immediately after the `<Config Name="Grafana Data" ...>...</Config>` line, insert the same two `<Config>` lines from Task 4 Step 1 (the `Grafana Dashboards (git)` and `Grafana Provisioning (git)` entries). Then validate:
```bash
python3 -c "import xml.dom.minidom; xml.dom.minidom.parse('/tmp/live-template.xml'); print('valid XML')"
```
Expected: `valid XML`. If invalid, STOP and fix before copying back.

- [ ] **Step 3: Copy the edited template back**

```bash
scp -i ~/.ssh/unraid_deploy -P 2222 /tmp/live-template.xml \
  root@192.168.1.133:/boot/config/plugins/dockerMan/templates-user/my-UNRAIDnotUnHealthy.xml
```

---

## Task 7: Recreate once + verify mounts live, dashboards intact

**Files:** none (host action)

- [ ] **Step 1: GATE — confirm with user before the recreate**

This is the one-time recreate that applies the new mounts. **Ask the user:** "Ready for the one-time recreate that activates the bind-mounts? Brief dashboards-offline blip; after this, config changes never need a recreate again."

- [ ] **Step 2: Recreate from the (now-edited) template**

```bash
ssh -i ~/.ssh/unraid_deploy -p 2222 root@192.168.1.133 \
  "php /usr/local/emhttp/plugins/dynamix.docker.manager/scripts/update_container 'UNRAIDnotUnHealthy' 2>&1 | tail -4"
```
(This is the same code path `deploy-unraid.sh` uses for the remote update.)

- [ ] **Step 3: Verify the new mounts are active**

```bash
ssh -i ~/.ssh/unraid_deploy -p 2222 root@192.168.1.133 \
  "docker inspect UNRAIDnotUnHealthy --format '{{range .Mounts}}{{.Source}} -> {{.Destination}} ({{.Mode}}){{println}}{{end}}' | grep -E 'etc/grafana'"
```
Expected: two lines mapping `…/config-repo/grafana/dashboards -> /etc/grafana/dashboards (ro)` and `…/grafana/provisioning -> /etc/grafana/provisioning (ro)`.

- [ ] **Step 4: Verify health + dashboards unchanged (render PNG)**

```bash
ssh -i ~/.ssh/unraid_deploy -p 2222 root@192.168.1.133 \
  "for i in \$(seq 1 24); do curl -s -m3 http://192.168.1.100:3000/api/health 2>/dev/null | grep -q database && break; sleep 5; done; \
   curl -s -u admin:admin -o /tmp/verify.png -w 'render http=%{http_code} bytes=%{size_download}\n' \
   'http://192.168.1.100:3000/render/d-solo/plex-media-analysis/plex-media-analysis?panelId=15&var-library=2&width=800&height=400&from=now-6h&to=now'"
```
Expected: health ok; `render http=200` with a multi-KB PNG. Pull it (`scp … /tmp/verify.png`) and confirm the Audio Codec bargauge still renders — proving the mounted config is being served correctly.

**Rollback if broken:** restore the template backup and recreate:
```bash
ssh -i ~/.ssh/unraid_deploy -p 2222 root@192.168.1.133 \
  "cp /boot/config/plugins/dockerMan/templates-user/my-UNRAIDnotUnHealthy.xml.bak-bindmount \
      /boot/config/plugins/dockerMan/templates-user/my-UNRAIDnotUnHealthy.xml && \
   php /usr/local/emhttp/plugins/dynamix.docker.manager/scripts/update_container 'UNRAIDnotUnHealthy'"
```

---

## Task 8: Acceptance test — prove the hot-reload loop

**Files:** a throwaway dashboard-title edit (reverted after)

- [ ] **Step 1: Make a trivial, visible dashboard change**

Edit `grafana/dashboards/plex-media.json` — change the dashboard `"title"` from `"Plex Media Analysis"` to `"Plex Media Analysis (hot-reload test)"`. Validate JSON:
```bash
cd /home/jztiger/UNRAIDnotUnHealthy
python3 -c "import json; json.load(open('grafana/dashboards/plex-media.json')); print('valid')"
git add grafana/dashboards/plex-media.json
git commit -m "test: hot-reload acceptance check"
```

- [ ] **Step 2: Deploy via the new fast path (NO recreate)**

```bash
cd /home/jztiger/UNRAIDnotUnHealthy
./scripts/deploy-dashboards.sh
```
Expected: push + host pull + health ok, in seconds.

- [ ] **Step 3: Confirm the change hot-reloaded (within ~30s)**

```bash
ssh -i ~/.ssh/unraid_deploy -p 2222 root@192.168.1.133 \
  "sleep 35; curl -s -u admin:admin 'http://192.168.1.100:3000/api/dashboards/uid/plex-media-analysis' | grep -o '\"title\":\"Plex Media Analysis[^\"]*\"' | head -1"
```
Expected: `"title":"Plex Media Analysis (hot-reload test)"` — proving the dashboard updated with **no recreate, no rebuild**.

- [ ] **Step 4: Revert the test change**

```bash
cd /home/jztiger/UNRAIDnotUnHealthy
git revert --no-edit HEAD
./scripts/deploy-dashboards.sh
```
Confirm the title reverts within ~30s (same check as Step 3, expecting plain `"Plex Media Analysis"`).

- [ ] **Step 5: Verify a provisioning change path (datasource restart)**

Confirm the `--restart-grafana` flag works without a recreate: touch a no-op comment in `grafana/provisioning/datasources/datasources.yml`, commit, then:
```bash
cd /home/jztiger/UNRAIDnotUnHealthy && git add grafana/provisioning/datasources/datasources.yml && git commit -m "test: provisioning reload path" && ./scripts/deploy-dashboards.sh --restart-grafana
```
Expected: pull + `s6-svc -r grafana` + health ok. Then `git revert --no-edit HEAD && ./scripts/deploy-dashboards.sh --restart-grafana`.

---

## Self-Review

**Spec coverage:**
- §3 bind mounts (dashboards + provisioning, ro) → Tasks 4/6 (template), 5 (clone), 7 (verify) ✓
- §3 image keeps baked fallback → unchanged (no Dockerfile edit; baked COPY stays) ✓
- §3 datasources minimal/UI-managed → unchanged (datasources.yml already minimal; Tautulli/Tdarr UI-managed) ✓
- §4 deploy-dashboards.sh → Task 3 ✓
- §5 deploy-unraid.sh IP fix → Task 2 ✓
- §2 file-first / allowUiUpdates:false → Task 1 ✓
- §6 one-time migration → Tasks 5/6/7 ✓
- §7 success criteria (hot-reload <1min, datasource via restart, render proof) → Task 8 ✓

**Placeholder scan:** No TBD/TODO. All commands + file contents concrete. The Task 4 `<Volume>` block is conditional ("if the template uses one") — that's a real branch (template format varies), with the mirror instruction given.

**Consistency:** `config-repo` path `/mnt/user/appdb/unraidnotunhealthy/config-repo` identical across Tasks 3-7. Container `/etc/grafana/{dashboards,provisioning}` targets consistent. `192.168.1.100` for Grafana health/render everywhere; `192.168.1.133:2222` for SSH everywhere. `deploy-dashboards.sh` flags (`--restart-grafana`) consistent between Task 3 definition and Task 8 use.

**Note:** No image rebuild in this plan — the recreate (Task 7) reuses the current image (`99958fb`, with the renderer) and only adds mounts. The baked dashboards/provisioning remain as the fallback the mounts shadow.
