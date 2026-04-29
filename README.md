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
| `/var/run/docker.sock` | `/var/run/docker.sock` (ro) | cadvisor + Alloy — container metadata + log streams |
| `/var/log`          | `/host/var/log` (ro)     | Alloy — syslog / messages / kern.log  |
| `/dev`              | `/dev`                   | smartctl_exporter — disk SMART        |
| `/mnt/user/appdata/unraidnotunhealthy/prometheus` | `/var/lib/prometheus` | TSDB persistence |
| `/mnt/user/appdata/unraidnotunhealthy/grafana`    | `/var/lib/grafana`    | Grafana DB persistence |
| `/mnt/user/logs/unraidnotunhealthy`                | `/var/lib/loki`       | Loki log store — under `logs` share for capacity headroom |

Container also needs `--pid=host` and `--cap-add=SYS_RAWIO` (IPMI / SMART).

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

## Unraid deployment (macvlan, static LAN IP)

Clone the repo to `/mnt/user/appdata/unraidnotunhealthy/` (or anywhere on
cache), then create `docker-compose.override.yml` from the included example:

```sh
mkdir -p /mnt/user/appdata/unraidnotunhealthy/{prometheus,grafana}
mkdir -p /mnt/user/logs/unraidnotunhealthy
cp docker-compose.override.yml.example docker-compose.override.yml
# edit the IP — pick something unused on your LAN
./scripts/docker-build.sh   # or: docker compose up -d --build
```

The override does two things:

1. Puts the container on a static LAN IP via macvlan (network defaults to
   `eth0`; confirm yours with `docker network ls`).
2. Bind-mounts the three persistent volumes through `/mnt/user/...` (FUSE
   shfs):
   - `prometheus-data` → `/mnt/user/appdata/unraidnotunhealthy/prometheus/`
   - `grafana-data` → `/mnt/user/appdata/unraidnotunhealthy/grafana/`
   - `loki-data` → `/mnt/user/logs/unraidnotunhealthy/`

   Loki lives under the `logs` user share rather than `appdata` to give log
   retention room to grow without competing with other apps' state.

Notes:
- The Unraid host itself cannot reach a container on its own macvlan — browse
  to Grafana from another machine on the LAN.
- All persistent state lives at the three host paths above. Nothing critical
  is stored inside the container.

## Project layout

```
.
├── Dockerfile               # multi-stage; pulls upstream binaries
├── docker-compose.yml       # for local dev
├── unraid-template.xml      # Community Apps template
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

All panels carry history (default 6h, configurable).

## Versions pinned

See `Dockerfile` `ARG` block at the top of the file.

## License

MIT — see [LICENSE](LICENSE).
