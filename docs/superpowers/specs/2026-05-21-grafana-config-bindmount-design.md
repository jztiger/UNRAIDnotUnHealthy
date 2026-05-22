# Grafana Config Bind-Mount Migration — Design

**Date:** 2026-05-21
**Status:** Draft, pending user review
**Goal:** Make Grafana dashboard/datasource changes deploy in seconds (git pull → hot-reload) instead of minutes (CI image rebuild + container recreate), with no downtime.

## 1. Problem

Today the UNRAIDnotUnHealthy image bakes **everything** at build time: binaries (Grafana, Prometheus, exporters, the renderer) **and** config (dashboards, provisioning, grafana.ini). Binaries change rarely; dashboards change constantly. Because both are baked, every dashboard tweak requires the full pipeline — `git push` → GitHub Actions image build → ghcr.io → Unraid pull + **container recreate** (minutes + a dashboards-offline blip). This friction dominated the 2026-05-21 Stage 0 work and will recur on every future dashboard change.

## 2. Principle

**Image = app (changes rarely). Bind-mount = config (changes often).**

| Stays baked in image | Moves to host bind-mount |
|---|---|
| Grafana/Prometheus/Loki/Alloy/exporter binaries | — |
| s6 services, grafana.ini, the image-renderer | — |
| Dashboards JSON, provisioning YAML | ✅ (also kept baked as fallback) |

Decisions made during brainstorming:
- **File-first**: dashboard JSON in git is the source of truth. Provisioning is **read-only** (`allowUiUpdates: false`) so the Grafana UI cannot drift from git. UI is view-only for provisioned dashboards.
- **Sync = host git checkout + pull**, driven by a new `deploy-dashboards.sh`.
- Repo is **public** → host pulls over anonymous HTTPS, no credentials.

## 3. Architecture

```
This repo (WSL)  ──git push──>  GitHub (public)  <──git pull──  Host checkout
/home/jztiger/UNRAIDnotUnHealthy                       /mnt/user/appdb/unraidnotunhealthy/config-repo
                                                                  │ bind-mount (ro), grafana subdirs
                                                                  ▼
   container /etc/grafana/dashboards   ← config-repo/grafana/dashboards
            /etc/grafana/provisioning  ← config-repo/grafana/provisioning
                                                                  │
                       dashboard provider updateIntervalSeconds:30 → JSON changes hot-reload (≤30s)
```

### Bind mounts (read-only)

| Host path | Container path | Reload behavior |
|---|---|---|
| `…/config-repo/grafana/dashboards` | `/etc/grafana/dashboards` | Hot-reload ≤30s (provider re-scan) |
| `…/config-repo/grafana/provisioning` | `/etc/grafana/provisioning` | Datasource/provider YAML changes need a `s6-svc -r grafana` (seconds), not a recreate |

Mounted **read-only** (`ro`): the container never writes these; git is the only writer. Host base: `/mnt/user/appdb/unraidnotunhealthy/` (array-backed, consistent with the existing Prometheus/Grafana mounts).

### Image still bakes the same files (fallback)

The Dockerfile's `COPY grafana/provisioning/` and `COPY grafana/dashboards/` stay. When the bind-mounts are present they shadow the baked copies; if the mounts are ever absent, the baked copies keep the image self-contained. Zero extra cost, removes a footgun.

### Datasources

The mounted `datasources.yml` keeps the current **minimal provisioned set**: Prometheus, Loki, Plex Library, Phase2 Progress. Tautulli and Tdarr stay **UI-created** in the persistent Grafana DB (provisioning a uid that already exists as a UI datasource crash-loops Grafana — learned 2026-05-21). Changing a provisioned datasource = edit YAML → pull → `s6-svc -r grafana`.

## 4. deploy-dashboards.sh (new)

Fast config-only deploy, analogous to `deploy-unraid.sh` but with **no CI and no recreate**:

```
1. (local) require clean tree on main; git push origin main
2. ssh host: git -C /mnt/user/appdb/unraidnotunhealthy/config-repo pull --ff-only
3. if --restart-grafana passed (datasource/provisioning change):
     ssh host: docker exec UNRAIDnotUnHealthy /command/s6-svc -r /run/service/grafana
4. verify: curl http://192.168.1.100:3000/api/health → database ok
5. (optional) render a panel PNG to confirm
```

Dashboards-only change → omit `--restart-grafana`; hot-reload picks it up in ≤30s. Default verification host is **192.168.1.100** (the macvlan IP).

## 5. Adjacent fix (approved)

`deploy-unraid.sh`: change `UNRAID_WEBUI_HOST` default `192.168.1.133` → `192.168.1.100` so its `/api/health` check stops giving false-negatives (Grafana is on the macvlan IP, not the management IP). Small, in-the-same-area cleanup.

## 6. One-time migration (the last config recreate, ever)

1. **Clone** the repo to the host: `git clone https://github.com/jztiger/UNRAIDnotUnHealthy.git /mnt/user/appdb/unraidnotunhealthy/config-repo` (checkout the deployed commit).
2. **Template**: add the two read-only bind mounts to `/boot/config/plugins/dockerMan/templates-user/my-UNRAIDnotUnHealthy.xml` (two `<Config Type="Path" Mode="ro">` entries).
3. **Recreate once** to apply the new mounts (`update_container`). This is the final recreate needed for config changes.
4. **Add** `deploy-dashboards.sh` to the repo (and pull it onto the host).
5. **Verify**: confirm the mounts are live, dashboards render unchanged, then edit a dashboard → `deploy-dashboards.sh` → confirm the change hot-reloads within 30s (render a panel PNG to prove it). Confirm a datasource edit + `--restart-grafana` works.

## 7. Success criteria

- A dashboard JSON change reaches the running Grafana via `deploy-dashboards.sh` in **< 1 minute, no recreate, no downtime**, verified by a rendered PNG.
- A datasource change deploys via pull + `s6-svc -r grafana` (no recreate).
- The image still builds and runs standalone (baked fallback intact).
- `deploy-unraid.sh` health check passes on `.100`.

## 8. Non-goals

- UI-based dashboard authoring / export-back (we chose file-first; `allowUiUpdates: false`).
- Auto-sync/gitops cron (explicit `deploy-dashboards.sh` step preferred for control).
- Moving binaries or grafana.ini out of the image (they belong there).
- Changing the CI/ghcr pipeline for binary/image changes (still the right path for those).

## 9. Risks & rollback

| Risk | Mitigation / rollback |
|---|---|
| Bad dashboard JSON pulled | Hot-reload shows a broken panel, not a crash. Fix JSON + pull again. No recreate. |
| Mount points at empty/missing host dir | Container falls back to baked dashboards (still present in image). |
| Host checkout diverges (manual edits on host) | `git -C config-repo reset --hard origin/main` re-syncs. |
| `git pull` fails on host (network/GitHub) | Old config keeps running; deploy-dashboards.sh reports the failure. |
| Provisioning YAML error after a datasource edit | `s6-svc -r grafana` surfaces it in logs; revert the YAML + pull. Other dashboards unaffected until restart. |
