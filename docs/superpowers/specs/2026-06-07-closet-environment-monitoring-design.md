# Closet Environment Monitoring — Design

**Date:** 2026-06-07
**Status:** Approved (design), pending implementation plan

## Goal

Surface the rack-closet environment on the existing **UPS / Power** dashboard by
pulling two new data sources:

1. An **AC Infinity Controller 69 Pro** (WiFi-connected) — temperature, humidity,
   VPD, and per-port fan power, via AC Infinity's cloud API.
2. A **UniFi Protect UP-Sense** ("Server Room") — temperature, humidity, light,
   and battery, via the official UniFi Protect Integration API on the NVR.

Read-only, metrics-only. No control/writes, no Bluetooth, no alert rules this round.

## Architecture

Two new stdlib-Python exporters, one per source, following the established
`ups-modbus-exporter` pattern exactly:

- Background poll thread → cached state under a lock → HTTP `/metrics` handler
  that serves the cached snapshot (never blocks a scrape on the upstream).
- Stays alive on any upstream failure and reports `<prefix>_scrape_success=0`
  (the "idle-visible" pattern shared with `bgw-nat-scraper`), so the dashboard
  shows a gap rather than going silent.
- Empty/unset credentials → exporter idles with `scrape_success=0`, never crashes.
- Stdlib only (`http.server`, `urllib`, `ssl`, `json`, `threading`). No new deps.

Each exporter ships:
- `rootfs/usr/local/bin/<name>` — the Python script.
- `rootfs/etc/s6-overlay/s6-rc.d/<name>/{run,type,dependencies.d/base}` — s6 longrun.
- `rootfs/etc/s6-overlay/s6-rc.d/user/contents.d/<name>` — service enablement.
- A Prometheus scrape job in `rootfs/etc/prometheus/prometheus.yml`.

### Exporter 1 — `acinfinity-exporter`

| Aspect | Value |
|---|---|
| Listen | `127.0.0.1:9687` (env `ACINFINITY_EXPORTER_ADDR` / `_PORT`) |
| Upstream | `http://www.acinfinityserver.com` (cloud, plain HTTP — see Security) |
| Auth | `ACINFINITY_EMAIL` + `ACINFINITY_PASSWORD` → `POST /api/user/appUserLogin` returns `appId` token |
| Data | `POST /api/user/devInfoListAll` (`userId` param, `token` header) |
| Poll | 60s (env `ACINFINITY_POLL_INTERVAL`) |

Login token is cached and re-fetched on 401/expiry. Password is truncated to 25
chars by the upstream API (documented quirk) — pass through as-is.

**Metrics** (gauges, labeled by `device` and `port`):
- `acinfinity_temperature_celsius`
- `acinfinity_humidity_percent`
- `acinfinity_vpd_kpa` (from `vpdnums`)
- `acinfinity_fan_power{device,port}` (current power level 0–10, field `speak`)
- `acinfinity_scrape_success`, `acinfinity_scrape_duration_seconds`,
  `acinfinity_scrape_age_seconds`

Raw upstream fields are ×100 (`temperature`, `humidity`, `vpdnums`); divide on
decode. Temperature/VPD unit (°C vs °F) is verified against the live device at
build time and normalized to Celsius/kPa in the exporter; the Grafana panel can
display °F via transform.

### Exporter 2 — `unifi-protect-exporter`

| Aspect | Value |
|---|---|
| Listen | `127.0.0.1:9688` (env `UNIFI_PROTECT_EXPORTER_ADDR` / `_PORT`) |
| Upstream | `https://<UNIFI_PROTECT_HOST>/proxy/protect/integration/v1/sensors` |
| Auth | header `X-API-KEY: <UNIFI_PROTECT_API_KEY>` |
| TLS | self-signed NVR cert → `ssl` context with verification disabled (local LAN, documented) |
| Poll | 60s (env `UNIFI_PROTECT_POLL_INTERVAL`) |

Verified live against NVR `192.168.0.159` running Protect **7.1.76**. The
`/sensors` endpoint returns an array; fields pinned:
`name`, `state`, `stats.temperature.value` (already °C), `stats.humidity.value`
(% or null), `stats.light.value` (lux or null), `batteryStatus.percentage`,
`wirelessConnectionState.signalState.signalStrength` (RSSI dBm). `null` stat
values are skipped (not emitted), matching the UPS exporter's `0xFFFF` handling.

**Metrics** (gauges, labeled by `name`): all 5 sensors are exported (trivial
cardinality); the dashboard filters to the closet.
- `unifi_sensor_temperature_celsius`
- `unifi_sensor_humidity_percent`
- `unifi_sensor_light_lux`
- `unifi_sensor_battery_percent`
- `unifi_sensor_signal_dbm`
- `unifi_sensor_scrape_success`, `unifi_sensor_scrape_duration_seconds`,
  `unifi_sensor_scrape_age_seconds`

## Configuration & secrets

New env vars added to `.env.example`, `docker-compose.yml`, and
`unraid-template.xml` (the template XML on the Unraid USB is the source of truth
per the deploy workflow). **Real credentials/keys are never committed** — repo
files carry placeholders only.

- `ACINFINITY_EMAIL`, `ACINFINITY_PASSWORD`
- `UNIFI_PROTECT_HOST` (e.g. `192.168.0.159`), `UNIFI_PROTECT_API_KEY`

The Protect API key shared during design should be rotated in Protect settings,
since it was transmitted in plaintext chat.

## Dashboard

A new **"Closet Environment"** row appended to `grafana/dashboards/ups.json`:
- Temperature timeseries — AC Infinity vs UP-Sense `Server Room` overlaid (cross-check).
- Humidity timeseries — both sources.
- VPD timeseries (AC Infinity).
- Fan-power gauge(s) per AC Infinity port.
- UP-Sense battery + signal stat tiles.
- Two `scrape_success` link tiles styled like the existing UPS "no link" tiles.

Default sensor focus: `Server Room` (the rack closet).

## Security notes

- AC Infinity cloud API is **plain HTTP with no TLS** — credentials traverse the
  network unencrypted. Recommend a dedicated AC Infinity account, not a reused
  password. This is an upstream limitation, not a project choice.
- UniFi Protect API key is read-only scoped and stays on the LAN over HTTPS.

## Out of scope (YAGNI)

- Writing/controlling AC Infinity ports or Protect devices.
- Bluetooth-local AC Infinity access.
- Alerting/recording rules (can follow once metrics are flowing).
- The non-closet UP-Sense units beyond being exported and selectable.

## Verification

- Each exporter: `curl 127.0.0.1:<port>/metrics` shows expected gauges and
  `scrape_success=1` against live upstreams; `scrape_success=0` (no crash) when
  creds are blank or upstream unreachable.
- Prometheus targets for both jobs report `up=1`.
- Grafana "Closet Environment" row renders both temperature sources.
