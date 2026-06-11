# Design: Relocate UP-Sense Collection to the Raspberry Pi (remote_write push)

**Date:** 2026-06-11
**Status:** Approved (brainstorming)

## Problem

The UniFi Protect UP-Sense in the rack closet reports to the Protect NVR at
`192.168.0.159` (on the Dream Wall LAN, `192.168.0.0/24`). The
`unraidnotunhealthy` monitoring container runs on the BGW LAN
(`192.168.1.0/24`, macvlan IP `192.168.1.100`). Traffic from `1.x → 0.x` is
blocked by the Dream Wall, so the in-container `unifi-protect-exporter` cannot
reach the NVR — it scrapes-fail forever (`urlopen timed out`, http 000). The
AC Infinity exporter is unaffected (it uses an outbound cloud API) and stays in
the container.

## Verified network facts

- This dev host and the Pi both sit on `192.168.0.0/24`. The Pi is
  `192.168.0.95`, always-on, running Docker.
- `0.x → 1.x` is open (outbound through the Dream Wall): from `0.x` we reach the
  container at `192.168.1.100` — sub-ms ping, `GET :3000/api/health` → 200.
- The container exposes **only** Grafana `:3000`. Prometheus (`:9090`) and both
  exporters (`:9687`/`:9688`) are loopback-bound inside the container, closed
  from the network.
- The Pi can reach the NVR `192.168.0.159:443` (443 open, sub-ms ping).

**Direction constraint:** Prometheus *pulls*, but `1.x → 0.x` is blocked, so the
container cannot scrape the Pi. The Pi must **initiate** the connection and
**push** data toward the container (`0.x → 1.x`, the allowed direction).

## Chosen approach

**remote_write push with basic auth (plain HTTP, LAN-trust).** Considered and
rejected: Pushgateway (freezes last values when the collector dies — wrong
failure mode for environmental data) and a reverse SSH tunnel (under macvlan it
would require adding `sshd` inside the appliance container — more surface than
the push it avoids).

## Architecture

```
UP-Sense ─▶ NVR(192.168.0.159) ─▶ unifi-protect-exporter (Pi loopback :9688)
                                        │ scrape (localhost)
                                        ▼
                                   vmagent (Pi)
                                        │ remote_write  (HTTP basic auth, 0.x→1.x)
                                        ▼
                          Prometheus (192.168.1.100:9090, receiver + basic auth)
                                        │ (loopback)
                                        ▼
                                   Grafana (:3000)
```

### On the Pi — new `docker-compose` stack (`pi/`)

1. **`unifi-protect-exporter`** — the **exact** script already in the repo
   (`rootfs/usr/local/bin/unifi-protect-exporter`), copied into a minimal
   `python:3-slim` image. Pure stdlib, no deps. Binds Pi-loopback `:9688`. Env:
   `UNIFI_PROTECT_HOST=192.168.0.159`, `UNIFI_PROTECT_API_KEY=<rotated key>`,
   `UNIFI_PROTECT_EXPORTER_PORT=9688`.
2. **`vmagent`** (VictoriaMetrics agent, lightweight) — scrapes
   `localhost:9688` on the standard interval, attaches `job="unifi_protect"` and
   an `instance` label, and `remote_write`s to
   `http://192.168.1.100:9090/api/v1/write` with basic auth.

Both run as containers via `pi/docker-compose.yml`. Image tags pinned to the
Pi's architecture (**confirm arm64 vs armhf at deploy** before pinning).

### On the container — changes to this repo

- **Prometheus** (`rootfs/etc/s6-overlay/s6-rc.d/prometheus/run`):
  - add `--web.enable-remote-write-receiver`
  - change `--web.listen-address=127.0.0.1:9090` → `0.0.0.0:9090` (so Grafana on
    loopback *and* the Pi both reach it)
  - add `--web.config.file=<path>` pointing at a basic-auth web-config.
- **Web-config generation** — a new s6 oneshot init writes the Prometheus
  web-config YAML (`basic_auth_users: { <user>: <bcrypt-hash> }`) from a
  template env var **before** Prometheus starts. The value is a bcrypt hash
  (`$2b$…`, full of `$`) — the init MUST write it **literally** (no shell/`${}`
  expansion; write via a quoted heredoc or Python).
- **Grafana datasource** (`grafana/provisioning/datasources/datasources.yml`) —
  add `basicAuth: true` + `basicAuthUser` / `secureJsonData.basicAuthPassword`
  so Grafana authenticates to the now-protected Prometheus. (Datasource keeps
  `url: http://127.0.0.1:9090`.)
- **Remove the in-container UniFi exporter:**
  - delete the `unifi-protect-exporter` s6 service dir + its
    `user/contents.d/unifi-protect-exporter` enable-marker
  - remove the failing `unifi_protect` scrape job from
    `rootfs/etc/prometheus/prometheus.yml` (data now arrives via remote_write)
  - move the two `UNIFI_PROTECT_*` env vars out of `docker-compose.yml`,
    `.env.example`, and `unraid-template.xml` (they belong on the Pi now). The
    exporter **script stays** in the repo — it's the source the Pi image builds
    from.

## Data flow & labels

vmagent stamps `job="unifi_protect"` (and an `instance`) so the remote-written
series are indistinguishable from a normal scrape in Grafana. The existing
`grafana/dashboards/ups.json` "Closet Environment" row already queries
`unifi_sensor_temperature_celsius{name="Server Room"}` — no dashboard change
needed; the series simply start arriving.

## Failure behavior

If the Pi/collector dies, remote_write stops → the series go **absent** (a
visible gap in Grafana), not frozen-stale like Pushgateway. Note: remote_write
does **not** synthesize an `up{job="unifi_protect"}` series, so there is no
`up==0` to alert on. Collector-down alerting (if added later) must use
`absent()` / staleness on `unifi_sensor_temperature_celsius`, or vmagent can
push a heartbeat metric. No alerting is built in this project (YAGNI).

## Security posture

- Transport is **plain HTTP + basic auth over the home LAN** (explicit user
  decision). The credential guards metric-*write*; the path is entirely the
  user's LAN. Residual risk documented: the basic-auth password is
  base64-reversible on the wire, and exposing `0.0.0.0:9090` makes an
  authenticated Prometheus reachable on the LAN (previously only Grafana was
  exposed). Acceptable under LAN-trust.
- Secrets:
  - **Pi** `.env` (gitignored): rotated Protect API key + basic-auth
    user/password (plaintext, Pi-side only).
  - **Container**: the **bcrypt hash** of the password as a masked
    `unraid-template.xml` env var + matching `docker-compose.yml` passthrough.
    Plaintext never lives in the container.
- The Protect API key pasted in plaintext earlier MUST be rotated; the Pi uses
  the new key. The leaked key is never written to the new host.

## Repo layout (new)

```
pi/
  docker-compose.yml     # exporter + vmagent services
  Dockerfile.exporter    # python:3-slim + copy of the shared exporter script
  vmagent.yml            # scrape localhost:9688 → remote_write to 1.100
  .env.example           # blank UNIFI_PROTECT_* + REMOTE_WRITE basic-auth creds
  README.md              # Pi deploy + rotation notes
```

## Deploy

- **Pi**: git-clones this repo; deploy = `git pull` +
  `docker compose -f pi/docker-compose.yml up -d --build`. SSH with a
  user/password provided at deploy time (entered via the `!` prefix, kept out of
  the transcript). The Pi `.env` is created from `.env.example` on the Pi (not
  committed).
- **Container**: standard project flow — push → CI → ghcr.io → ssh + `update_container` for the
  image; config-repo `git pull` + `s6-svc -r grafana` for the
  datasource/dashboard bind-mount.

### Deploy ordering (must not blank the dashboards)

The Prometheus auth/bind change ships in the **image**; the Grafana
datasource-creds change ships via the **config-repo git-pull + grafana
restart** — different mechanisms. If auth turns on before the datasource has
creds, Grafana gets 401 on every query and **all** dashboards (system, disks,
GPU, IPMI — not just the closet) go blank. Safe order, zero breakage window:

1. **Datasource creds first** — config-repo `git pull` + `s6-svc -r grafana`. A
   creds-bearing datasource against a not-yet-auth Prometheus still works
   (Prometheus ignores the unexpected auth header).
2. **Then deploy the auth-on image** — push → CI → `update_container`.
3. **Then bring up the Pi stack** — it can start any time after the receiver is
   live.

## Testing / verification

- **Pi**: exporter `/metrics` shows live
  `unifi_sensor_temperature_celsius{name="Server Room"}`; vmagent's own target
  page shows the exporter UP and remote_write succeeding (HTTP 2xx).
- **Container**: `curl -u <user>:<pass>` confirms remote-written samples land
  (`/api/v1/query?query=unifi_sensor_temperature_celsius`); an unauthenticated
  request is rejected (401). Grafana "Closet Environment" Temperature panel
  fills with the Server Room series.
- **Regression**: after the auth-on image deploy, confirm the existing
  system/disk/GPU dashboards still render (datasource creds working).

## Out of scope (YAGNI)

- AC Infinity control / write path (separate future project).
- Alerting on collector-down (noted as a future option only).
- TLS / cert management (LAN-trust HTTP chosen).
