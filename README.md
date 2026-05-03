# UNRAIDnotUnHealthy

A single-container, all-in-one Prometheus + Grafana monitoring stack for
[Unraid](https://unraid.net). One install, one port, comprehensive
dashboards covering system, disks/SMART, Docker containers, GPU, and IPMI.

> If your Unraid box is healthy, this dashboard tells you so.
> If it isn't, this dashboard tells you *exactly* what's wrong.

## What's inside

A single Docker image bundles everything, supervised by
[s6-overlay](https://github.com/just-containers/s6-overlay):

| Component                | Purpose                                           |
| ------------------------ | ------------------------------------------------- |
| Prometheus               | Time-series database + scraper                    |
| Grafana                  | Dashboards (pre-provisioned)                      |
| Loki                     | Log store (queried alongside metrics in Grafana)  |
| Grafana Alloy            | Log collector — Docker containers + host syslog   |
| `node_exporter`          | CPU, RAM, network, filesystems, host temps        |
| `cadvisor`               | Per-container CPU/RAM/network/IO                  |
| `smartctl_exporter`      | Per-disk SMART attributes, temps, spinup state    |
| `nvidia_gpu_exporter`    | NVIDIA GPU utilisation, VRAM, temp, power         |
| `ipmi_exporter`          | Motherboard sensors, fan RPM, power supply health |

All exporters bind to `127.0.0.1`. Only Grafana (port `3000`) is exposed.

## Quick start (Unraid)

1. Add the template via Community Apps (or import `unraid-template.xml`).
2. Set the host paths in the template (see **Required mounts** below).
3. Apply. Open `http://<tower-ip>:3000` — default login `admin / admin`.
4. Five dashboards land in the **UNRAIDnotUnHealthy** folder automatically.

### Required mounts

| Host path           | Container path           | Why                                   |
| ------------------- | ------------------------ | ------------------------------------- |
| `/`                 | `/rootfs` (ro)           | node_exporter filesystem stats        |
| `/proc`             | `/host/proc` (ro)        | node_exporter / cadvisor              |
| `/sys`              | `/host/sys` (ro)         | node_exporter / cadvisor              |
| `/sys`              | `/sys` (ro)              | cAdvisor — per-container cgroup enumeration |
| `/var/lib/docker`   | `/var/lib/docker` (ro)   | cAdvisor — container metadata join |
| `/var/run/docker.sock` | `/var/run/docker.sock` (ro) | cadvisor + Alloy — container metadata + log streams |
| `/var/log`          | `/host/var/log` (ro)     | Alloy — syslog / messages / kern.log  |
| `/dev`              | `/dev`                   | smartctl_exporter — disk SMART        |
| `/mnt/user/appdata/unraidnotunhealthy/prometheus` | `/var/lib/prometheus` | TSDB persistence |
| `/mnt/user/appdata/unraidnotunhealthy/grafana`    | `/var/lib/grafana`    | Grafana DB persistence |
| `/mnt/user/logs/unraidnotunhealthy`                | `/var/lib/loki`       | Loki log store — under `logs` share for capacity headroom |
| `/mnt/user/plexmedia/dbexport` *(optional)* | `/var/lib/grafana/plex_data` (rw) | Plex library snapshot for the **Plex Media Analysis** dashboard. Produced by the User Scripts entry `plex_media_analysis` (daily). Mount can be omitted on hosts without Plex — the dashboard simply shows "no data". `rw` is needed because SQLite refuses to open without journal-file creation rights; the dashboard only issues SELECTs and the daily snapshot overwrites atomically. |

Container needs `--privileged` (SMART + IPMI raw device access — same
pattern as scrutiny and other hardware-monitoring containers).

### Environment variables

| Var | Default | Notes |
| --- | --- | --- |
| `TZ` | `UTC` | IANA timezone, e.g. `America/Los_Angeles`. |
| `PROMETHEUS_RETENTION` | `30d` | TSDB retention window. Bigger = more disk. |
| `LOKI_RETENTION` | `14d` | Log retention window. Logs use disk fast — bump only if you have headroom. |
| `PUID` / `PGID` | `1000` / `1000` (Unraid template: `99` / `100`) | Remap the in-container `unhealthy` user to a host uid/gid so persistent volumes don't end up root-owned. |
| `GF_SECURITY_ADMIN_PASSWORD` | unset | Initial Grafana admin password. If unset, Grafana defaults to `admin` and prompts a change on first login. |

### NVIDIA GPU (optional)

If you have the [Nvidia-Driver](https://forums.unraid.net/topic/98978-plugin-nvidia-driver/)
plugin installed, set `--runtime=nvidia` and pass `NVIDIA_VISIBLE_DEVICES=all`.
Without these, the GPU exporter starts but reports no devices — harmless.

### IPMI (optional)

If your motherboard exposes IPMI, the exporter uses `freeipmi` against
`/dev/ipmi0`. If your board lacks IPMI, the exporter logs a warning and
the IPMI dashboard panels show "no data" — also harmless.

## Local development

```sh
./scripts/docker-build.sh        # version-stamped build + up -d
# or
docker compose up --build        # plain — version reports as 'unknown'
```

Then `http://localhost:3000`. Build version (commit + count + UTC time) is
visible via `docker inspect unraidnotunhealthy --format '{{json .Config.Labels}}'`
or the `BUILD_*` env vars inside the container.

### Push a new image to your Unraid host

```sh
bash scripts/deploy-unraid.sh    # push → wait for CI → ssh + update_container → verify
```

The script pushes to `main`, waits for the GitHub Actions workflow to build and publish `ghcr.io/jztiger/unraidnotunhealthy:latest`, SSHes to Unraid (`root@192.168.1.133:2222`), and invokes `php /usr/local/emhttp/plugins/dynamix.docker.manager/scripts/update_container UNRAIDnotUnHealthy` — the same code path as clicking **Update** in the Unraid Docker tab. Health check via Grafana's `/api/health`.

End-to-end is typically 60-90s once the GHA layer cache is warm; first cold build ~3-5 min.

## Unraid deployment (native template)

Production runs under Unraid's native template management. The template at `unraid-template.xml` in this repo is the source of truth — copy it to `/boot/config/plugins/dockerMan/templates-user/my-UNRAIDnotUnHealthy.xml` on the Unraid USB, then in the Docker tab click **Add Container** → User templates → **UNRAIDnotUnHealthy**.

Key settings the template ships with:

- `<Repository>ghcr.io/jztiger/unraidnotunhealthy:latest</Repository>` — pulled from GHCR, not built on the box
- Network: `bridge` (Grafana on `<unraid-ip>:3000`)
- Persistent paths default to:
  - **Prometheus TSDB** → `/mnt/user/appdb/unraidnotunhealthy/prometheus` (array-only, grows with retention)
  - **Grafana DB** → `/mnt/user/appdb/unraidnotunhealthy/grafana` (array-only)
  - **Loki logs** → `/mnt/user/logs/unraidnotunhealthy` (array-only)

  Putting the growing TSDBs on `appdb` (array share) instead of `appdata` (cache pool) keeps the cache from filling over time. Override any of these in the Edit form if your share layout differs.

- Required host bind mounts (read-only): `/`, `/proc`, `/sys`, `/var/lib/docker`, `/var/run/docker.sock`, `/var/log`. Read-write `/dev` for SMART + IPMI.

Since the image is published to a private GHCR package, Unraid needs `docker login ghcr.io` once with a PAT that has `read:packages` scope. Persist by saving `/root/.docker/config.json` to `/boot/config/docker-auth/` and restoring it via `/boot/config/go` (Unraid's `/root` is tmpfs).

For updates: click **Update** in the Docker tab any time, or run `bash scripts/deploy-unraid.sh` from the dev box.

## Project layout

```
.
├── Dockerfile               # multi-stage; pulls upstream binaries
├── docker-compose.yml       # for local dev
├── unraid-template.xml      # Unraid template (source of truth for production deploy)
├── rootfs/                  # baked into image (s6 services + configs)
│   └── etc/
│       ├── s6-overlay/...   # one longrun service per exporter
│       └── prometheus/prometheus.yml
└── grafana/
    ├── provisioning/        # datasource + dashboard providers
    └── dashboards/          # JSON dashboards bundled in image
```

## Dashboards

| Dashboard      | What it shows                                                          |
| -------------- | ---------------------------------------------------------------------- |
| **Overview**   | Single-screen health: load, RAM, network, container count, alert tally |
| **Disks/SMART**| Per-disk temps, hours, reallocated/pending sectors, self-test results  |
| **Containers** | Per-container CPU/RAM/network/IO with selector                         |
| **GPU**        | Util %, VRAM used, temp, power draw, encoder/decoder load              |
| **IPMI**       | Fan RPMs, motherboard temps, voltages, PSU health                      |
| **Logs**       | Live tail + search across Docker containers and host syslog (Loki)     |
| **Sonarr & Radarr** | Library size, missing, queue, quality breakdown, health, root-folder space — drill-downs link to the *arr UIs. Requires `SONARR_API_KEY` / `RADARR_API_KEY` env. |
| **Plex Media Analysis** | Codec, resolution, HDR/Dolby Vision, audio codec/channels/Atmos, container, bitrate distribution, top-bitrate movies, drill-down table. Reads a daily SQLite snapshot at `/var/lib/grafana/plex_data/plex_snapshot.db` (mount optional — dashboard quietly shows "no data" without it). |

All panels carry history (default 6h, configurable). The Plex Media Analysis dashboard is point-in-time (refreshes once a day with the snapshot).

## Versions pinned

See `Dockerfile` `ARG` block at the top of the file.

## License

MIT — see [LICENSE](LICENSE).
