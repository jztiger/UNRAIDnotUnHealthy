# Pi UP-Sense Collector (remote_write push) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move UP-Sense collection off the unreachable in-container exporter onto the Raspberry Pi (192.168.0.95), which scrapes the NVR locally and `remote_write`s to the container's macvlan Prometheus (192.168.1.100) over HTTP basic auth.

**Architecture:** A new `pi/` docker-compose stack runs the *existing* `unifi-protect-exporter` script + a `vmagent` shipper. The container's Prometheus gains a remote-write receiver, binds `0.0.0.0:9090`, and enables **opt-in** basic auth (only when the password env is set) via a generated web-config. Grafana's datasource carries matching (blank-safe) creds. The in-container UniFi exporter is removed; its script stays as the Pi build source.

**Tech Stack:** s6-overlay (oneshot + longrun services), Prometheus 3.1.0 (`--web.config.file` basic auth, `--web.enable-remote-write-receiver`), Grafana provisioning (`$__env{}` interpolation), VictoriaMetrics `vmagent`, Docker Compose, stdlib-Python exporter.

---

## Key design decisions (read before starting)

- **Auth is opt-in.** The web-config generator emits `basic_auth_users` **only** when `PROMETHEUS_REMOTE_WRITE_PASSWORD_BCRYPT` is non-empty; otherwise it writes an empty config (`{}` = no auth). This means deploying the image with the env unset changes nothing, removing the hard ordering risk from the spec.
- **bcrypt hash is generated at deploy time** (not at container start — avoids depending on the deprecated Python `crypt` module) and stored as a masked template env var. The init writes it **literally** with `printf %s` (no shell/`${}` expansion that would mangle the `$2b$…` `$` characters).
- **Two correlated secrets on the container:** `PROMETHEUS_REMOTE_WRITE_PASSWORD_BCRYPT` (for Prometheus, can't be reversed) and `PROM_BASIC_AUTH_PASSWORD` (plaintext, for Grafana to send). Both masked. Same underlying password.
- **The `prometheus` self-scrape job needs creds too** once auth is on — handled with a `basic_auth` block that reads the same `$__env` style is not available in prometheus.yml, so it uses a small static block fed by the web-config user/pass. See Task 2 Step 7.
- **The exporter script is the single source of truth** at `rootfs/usr/local/bin/unifi-protect-exporter`; the Pi image `COPY`s it from the repo-root build context. Do **not** fork it into `pi/`.

---

## File structure

**New (`pi/` — the Raspberry Pi stack):**
- `pi/docker-compose.yml` — `unifi-protect-exporter` + `vmagent` services
- `pi/Dockerfile.exporter` — `python:3-slim` + COPY of the shared exporter script
- `pi/vmagent-scrape.yml` — vmagent's scrape config (scrapes the exporter)
- `pi/.env.example` — blank `UNIFI_PROTECT_*` + `REMOTE_WRITE_*` creds
- `pi/.gitignore` — ignores `.env`
- `pi/README.md` — deploy + key-rotation runbook

**New (container init):**
- `rootfs/etc/cont-init.d/03-prometheus-web-config` — generates the web-config
- `rootfs/etc/s6-overlay/s6-rc.d/init-prometheus-web-config/{up,type,dependencies.d/base}` — oneshot wrapping it
- `rootfs/etc/s6-overlay/s6-rc.d/user/contents.d/init-prometheus-web-config` — enable marker

**Modified (container):**
- `rootfs/etc/s6-overlay/s6-rc.d/prometheus/run` — new flags
- `rootfs/etc/s6-overlay/s6-rc.d/prometheus/dependencies.d/init-prometheus-web-config` — new dep marker (file)
- `rootfs/etc/prometheus/prometheus.yml` — remove `unifi_protect` job, add `basic_auth` to `prometheus` job
- `grafana/provisioning/datasources/datasources.yml` — basic auth on the Prometheus datasource
- `docker-compose.yml` / `.env.example` / `unraid-template.xml` — drop `UNIFI_PROTECT_*`, add `PROMETHEUS_REMOTE_WRITE_USER` / `PROMETHEUS_REMOTE_WRITE_PASSWORD_BCRYPT` / `PROM_BASIC_AUTH_PASSWORD`

**Removed (container):**
- `rootfs/etc/s6-overlay/s6-rc.d/unifi-protect-exporter/` (whole dir)
- `rootfs/etc/s6-overlay/s6-rc.d/user/contents.d/unifi-protect-exporter` (enable marker)

**Unchanged (kept as Pi build source):**
- `rootfs/usr/local/bin/unifi-protect-exporter`
- `tests/test_unifi_protect_exporter.py`

---

## Task 1: Scaffold the `pi/` collector stack

**Files:**
- Create: `pi/Dockerfile.exporter`
- Create: `pi/vmagent-scrape.yml`
- Create: `pi/docker-compose.yml`
- Create: `pi/.env.example`
- Create: `pi/.gitignore`

- [ ] **Step 1: Create the exporter Dockerfile**

`pi/Dockerfile.exporter`:

```dockerfile
# Builds the UP-Sense exporter image for the Raspberry Pi collector.
# Build context is the REPO ROOT so we reuse the single source-of-truth script
# at rootfs/usr/local/bin/unifi-protect-exporter (no fork). Stdlib-only, so the
# slim base needs no extra packages. Multi-arch base — resolves to arm64/armhf
# automatically on the Pi.
FROM python:3-slim
COPY rootfs/usr/local/bin/unifi-protect-exporter /usr/local/bin/unifi-protect-exporter
RUN chmod 0755 /usr/local/bin/unifi-protect-exporter
# Bind all interfaces inside the container so the sibling vmagent container can
# reach it by compose service name. Not published to the Pi host.
ENV UNIFI_PROTECT_EXPORTER_ADDR=0.0.0.0
EXPOSE 9688
ENTRYPOINT ["/usr/local/bin/unifi-protect-exporter"]
```

- [ ] **Step 2: Create vmagent's scrape config**

`pi/vmagent-scrape.yml`:

```yaml
# vmagent scrapes the local exporter and remote_writes to the container's
# Prometheus (configured via -remoteWrite.* flags in docker-compose.yml).
# job_name becomes the `job` label so the series look like a normal scrape in
# Grafana; instance marks them as Pi-sourced.
global:
  scrape_interval: 60s
scrape_configs:
  - job_name: unifi_protect
    static_configs:
      - targets: ['unifi-protect-exporter:9688']
        labels:
          instance: closet-pi
```

- [ ] **Step 3: Create the Pi compose file**

`pi/docker-compose.yml`:

```yaml
# Raspberry Pi (192.168.0.95) closet collector. Runs the UP-Sense exporter
# (reaches the NVR at 192.168.0.159, which the Unraid container cannot) and a
# vmagent that remote_writes to the container's Prometheus at 192.168.1.100.
# Deploy: git pull && docker compose -f pi/docker-compose.yml up -d --build
services:
  unifi-protect-exporter:
    build:
      context: ..                       # repo root, to COPY the shared script
      dockerfile: pi/Dockerfile.exporter
    image: closet-unifi-protect-exporter:local
    container_name: unifi-protect-exporter
    restart: unless-stopped
    environment:
      UNIFI_PROTECT_HOST: ${UNIFI_PROTECT_HOST:-192.168.0.159}
      UNIFI_PROTECT_API_KEY: ${UNIFI_PROTECT_API_KEY:-}
      UNIFI_PROTECT_EXPORTER_ADDR: 0.0.0.0
    # No ports: published — only the sibling vmagent needs it.

  vmagent:
    image: victoriametrics/vmagent:v1.115.0   # multi-arch (arm64/armhf)
    container_name: closet-vmagent
    restart: unless-stopped
    depends_on:
      - unifi-protect-exporter
    command:
      - -promscrape.config=/etc/vmagent/scrape.yml
      - -remoteWrite.url=http://192.168.1.100:9090/api/v1/write
      - -remoteWrite.basicAuth.username=${REMOTE_WRITE_USER:-}
      - -remoteWrite.basicAuth.password=${REMOTE_WRITE_PASSWORD:-}
    volumes:
      - ./vmagent-scrape.yml:/etc/vmagent/scrape.yml:ro
```

- [ ] **Step 4: Create `.env.example` and `.gitignore`**

`pi/.env.example`:

```bash
# Raspberry Pi closet collector — copy to pi/.env on the Pi and fill in.
# pi/.env is gitignored; never commit real values.

# UniFi Protect NVR (UP-Sense). Use the ROTATED key — the one pasted earlier in
# chat is compromised and must not be reused.
UNIFI_PROTECT_HOST=192.168.0.159
UNIFI_PROTECT_API_KEY=

# remote_write basic auth — must match the container's
# PROMETHEUS_REMOTE_WRITE_USER and the plaintext behind its bcrypt hash.
REMOTE_WRITE_USER=closet-pi
REMOTE_WRITE_PASSWORD=
```

`pi/.gitignore`:

```gitignore
.env
```

- [ ] **Step 5: Validate the compose file**

Run: `docker compose -f pi/docker-compose.yml config -q && echo OK`
Expected: prints `OK` with no errors (interpolation + schema valid). A missing `.env` is fine — defaults cover it.

- [ ] **Step 6: Commit**

```bash
git add pi/Dockerfile.exporter pi/vmagent-scrape.yml pi/docker-compose.yml pi/.env.example pi/.gitignore
git commit -m "Add pi/ collector stack: UP-Sense exporter + vmagent remote_write"
```

---

## Task 2: Prometheus — remote-write receiver, 0.0.0.0 bind, opt-in basic auth

**Files:**
- Create: `rootfs/etc/cont-init.d/03-prometheus-web-config`
- Create: `rootfs/etc/s6-overlay/s6-rc.d/init-prometheus-web-config/up`
- Create: `rootfs/etc/s6-overlay/s6-rc.d/init-prometheus-web-config/type`
- Create: `rootfs/etc/s6-overlay/s6-rc.d/init-prometheus-web-config/dependencies.d/base`
- Create: `rootfs/etc/s6-overlay/s6-rc.d/user/contents.d/init-prometheus-web-config`
- Create: `rootfs/etc/s6-overlay/s6-rc.d/prometheus/dependencies.d/init-prometheus-web-config`
- Modify: `rootfs/etc/s6-overlay/s6-rc.d/prometheus/run`
- Modify: `rootfs/etc/prometheus/prometheus.yml`
- Test: `tests/test_prometheus_web_config.py`

- [ ] **Step 1: Write the failing test for the web-config generator**

`tests/test_prometheus_web_config.py`:

```python
"""Tests for the Prometheus web-config generator (cont-init script).

It must (a) emit basic_auth_users only when a bcrypt hash is set, (b) write the
hash LITERALLY (bcrypt's `$2b$...$` must survive — no shell expansion), and
(c) emit a valid no-auth config when the hash is empty.
"""
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "rootfs/etc/cont-init.d/03-prometheus-web-config"


def _run(tmp_path, env):
    out = tmp_path / "web-config.yml"
    full_env = {**os.environ, "PROM_WEB_CONFIG_PATH": str(out), **env}
    subprocess.run(["bash", str(SCRIPT)], env=full_env, check=True)
    return out.read_text()


def test_emits_basic_auth_when_hash_set(tmp_path):
    bcrypt = "$2b$10$abcdefghijklmnopqrstuv0123456789012345678901234567890ab"
    text = _run(tmp_path, {
        "PROMETHEUS_REMOTE_WRITE_USER": "closet-pi",
        "PROMETHEUS_REMOTE_WRITE_PASSWORD_BCRYPT": bcrypt,
    })
    assert "basic_auth_users:" in text
    assert "closet-pi:" in text
    # Hash must appear verbatim, $-chars intact.
    assert bcrypt in text


def test_no_auth_when_hash_empty(tmp_path):
    text = _run(tmp_path, {
        "PROMETHEUS_REMOTE_WRITE_USER": "closet-pi",
        "PROMETHEUS_REMOTE_WRITE_PASSWORD_BCRYPT": "",
    })
    assert "basic_auth_users" not in text
    assert text.strip() == "{}"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_prometheus_web_config.py -v`
Expected: FAIL — script does not exist (`No such file or directory`).

- [ ] **Step 3: Write the web-config generator**

`rootfs/etc/cont-init.d/03-prometheus-web-config`:

```bash
#!/usr/bin/env bash
# Generate Prometheus' web-config (basic auth) before it starts. Auth is
# OPT-IN: only when PROMETHEUS_REMOTE_WRITE_PASSWORD_BCRYPT is set do we emit a
# basic_auth_users block; otherwise we write an empty (no-auth) config so
# deployments without the env var are unaffected.
#
# The bcrypt hash ($2b$...) is written with printf %s so the `$` characters are
# inserted literally — never run it through shell/`${}` expansion or a YAML
# templater that would mangle them.
set -eu

OUT="${PROM_WEB_CONFIG_PATH:-/run/prometheus/web-config.yml}"
USER="${PROMETHEUS_REMOTE_WRITE_USER:-closet-pi}"
HASH="${PROMETHEUS_REMOTE_WRITE_PASSWORD_BCRYPT:-}"

install -d -m 0755 "$(dirname "$OUT")"

if [ -n "$HASH" ]; then
  {
    printf 'basic_auth_users:\n'
    printf '  %s: ' "$USER"
    printf '%s\n' "$HASH"
  } > "$OUT"
else
  printf '{}\n' > "$OUT"
fi
chmod 0644 "$OUT"
```

Make it executable:

Run: `chmod 0755 rootfs/etc/cont-init.d/03-prometheus-web-config`

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_prometheus_web_config.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Create the oneshot s6 service that runs the generator**

`rootfs/etc/s6-overlay/s6-rc.d/init-prometheus-web-config/up`:

```
/command/with-contenv /etc/cont-init.d/03-prometheus-web-config
```

`rootfs/etc/s6-overlay/s6-rc.d/init-prometheus-web-config/type`:

```
oneshot
```

(Write `type` with no trailing newline — match the sibling services. Use `printf 'oneshot' > …`.)

Create the dependency + enable markers (empty files):

Run:
```bash
install -d rootfs/etc/s6-overlay/s6-rc.d/init-prometheus-web-config/dependencies.d
touch rootfs/etc/s6-overlay/s6-rc.d/init-prometheus-web-config/dependencies.d/base
touch rootfs/etc/s6-overlay/s6-rc.d/user/contents.d/init-prometheus-web-config
```

- [ ] **Step 6: Make prometheus depend on the generator**

Run: `touch rootfs/etc/s6-overlay/s6-rc.d/prometheus/dependencies.d/init-prometheus-web-config`

This guarantees the web-config exists before Prometheus starts.

- [ ] **Step 7: Update the prometheus run script**

Replace `rootfs/etc/s6-overlay/s6-rc.d/prometheus/run` with:

```bash
#!/command/with-contenv bash
exec s6-setuidgid unhealthy /usr/local/bin/prometheus \
  --config.file=/etc/prometheus/prometheus.yml \
  --storage.tsdb.path=/var/lib/prometheus \
  --storage.tsdb.retention.time=${PROMETHEUS_RETENTION:-30d} \
  --web.listen-address=0.0.0.0:9090 \
  --web.enable-lifecycle \
  --web.enable-remote-write-receiver \
  --web.config.file=/run/prometheus/web-config.yml
```

- [ ] **Step 8: Update prometheus.yml — self-scrape auth + remove unifi job**

In `rootfs/etc/prometheus/prometheus.yml`, change the `prometheus` self-scrape job so it authenticates when auth is on. Prometheus.yml has no env interpolation, so reference a file the init also writes. Update the generator first:

Append to `rootfs/etc/cont-init.d/03-prometheus-web-config` (before the final `chmod`), a second output for the self-scrape creds:

```bash
# Also drop a password file the self-scrape job reads (kept in sync with the
# web-config). Empty password => Prometheus sends blank creds, which a no-auth
# server ignores, so this is blank-safe when auth is off.
PWFILE="${PROM_SELF_SCRAPE_PW_FILE:-/run/prometheus/self_scrape_password}"
printf '%s' "${PROM_BASIC_AUTH_PASSWORD:-}" > "$PWFILE"
# 0644 so Prometheus (runs as the unhealthy user, not root) can read it. The
# file lives in the container's tmpfs /run, never on a shared mount.
chmod 0644 "$PWFILE"
```

Then edit the `prometheus` job in `prometheus.yml`:

```yaml
  - job_name: prometheus
    basic_auth:
      username: closet-pi
      password_file: /run/prometheus/self_scrape_password
    static_configs:
      - targets: ['127.0.0.1:9090']
```

And **delete** the entire `unifi_protect` job block (the comment + job, ~6 lines ending at `targets: ['127.0.0.1:9688']`).

> Note: `username: closet-pi` must equal `PROMETHEUS_REMOTE_WRITE_USER`. `closet-pi` is the fixed default used throughout this plan; if you change the user, change it here too.

- [ ] **Step 9: Update the web-config test for the self-scrape password file**

Add to `tests/test_prometheus_web_config.py`:

```python
def test_writes_self_scrape_password_file(tmp_path):
    pw = tmp_path / "pw"
    out = tmp_path / "web-config.yml"
    env = {
        **os.environ,
        "PROM_WEB_CONFIG_PATH": str(out),
        "PROM_SELF_SCRAPE_PW_FILE": str(pw),
        "PROMETHEUS_REMOTE_WRITE_USER": "closet-pi",
        "PROMETHEUS_REMOTE_WRITE_PASSWORD_BCRYPT": "$2b$10$x",
        "PROM_BASIC_AUTH_PASSWORD": "s3cret",
    }
    subprocess.run(["bash", str(SCRIPT)], env=env, check=True)
    assert pw.read_text() == "s3cret"
```

Run: `pytest tests/test_prometheus_web_config.py -v`
Expected: PASS (all three tests).

- [ ] **Step 10: Validate prometheus.yml syntax**

Run: `docker run --rm -v "$PWD/rootfs/etc/prometheus/prometheus.yml:/p.yml:ro" --entrypoint promtool prom/prometheus:v3.1.0 check config /p.yml`
Expected: `SUCCESS: /p.yml is valid prometheus config file`. (The `password_file` need not exist for `check config`.)

- [ ] **Step 11: Commit**

```bash
git add rootfs/etc/cont-init.d/03-prometheus-web-config \
        rootfs/etc/s6-overlay/s6-rc.d/init-prometheus-web-config \
        rootfs/etc/s6-overlay/s6-rc.d/user/contents.d/init-prometheus-web-config \
        rootfs/etc/s6-overlay/s6-rc.d/prometheus \
        rootfs/etc/prometheus/prometheus.yml \
        tests/test_prometheus_web_config.py
git commit -m "Prometheus: remote-write receiver + opt-in basic auth, bind 0.0.0.0"
```

---

## Task 3: Grafana datasource basic auth (blank-safe)

**Files:**
- Modify: `grafana/provisioning/datasources/datasources.yml`

- [ ] **Step 1: Add basic auth to the Prometheus datasource**

In `grafana/provisioning/datasources/datasources.yml`, change the Prometheus datasource block to:

```yaml
  - name: Prometheus
    uid: unhealthy-prom
    type: prometheus
    access: proxy
    url: http://127.0.0.1:9090
    isDefault: true
    editable: false
    basicAuth: true
    basicAuthUser: $__env{PROMETHEUS_REMOTE_WRITE_USER}
    jsonData:
      timeInterval: 15s
    secureJsonData:
      basicAuthPassword: $__env{PROM_BASIC_AUTH_PASSWORD}
```

This is blank-safe: when the env vars are unset, Grafana sends empty basic auth, which a no-auth Prometheus ignores. When auth is on, the creds match.

- [ ] **Step 2: Validate YAML**

Run: `python3 -c "import yaml,sys; yaml.safe_load(open('grafana/provisioning/datasources/datasources.yml')); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add grafana/provisioning/datasources/datasources.yml
git commit -m "Grafana: basic-auth creds for the Prometheus datasource (env-interpolated)"
```

---

## Task 4: Remove the in-container UniFi exporter; rewire env vars

**Files:**
- Delete: `rootfs/etc/s6-overlay/s6-rc.d/unifi-protect-exporter/` (dir)
- Delete: `rootfs/etc/s6-overlay/s6-rc.d/user/contents.d/unifi-protect-exporter`
- Modify: `docker-compose.yml`
- Modify: `.env.example`
- Modify: `unraid-template.xml`

- [ ] **Step 1: Remove the s6 service + enable marker**

Run:
```bash
git rm -r rootfs/etc/s6-overlay/s6-rc.d/unifi-protect-exporter
git rm rootfs/etc/s6-overlay/s6-rc.d/user/contents.d/unifi-protect-exporter
```

- [ ] **Step 2: Swap env vars in docker-compose.yml**

In `docker-compose.yml`, delete these two lines:

```yaml
      UNIFI_PROTECT_HOST: ${UNIFI_PROTECT_HOST:-}
      UNIFI_PROTECT_API_KEY: ${UNIFI_PROTECT_API_KEY:-}
```

and add (in the same `environment:` block):

```yaml
      PROMETHEUS_REMOTE_WRITE_USER: ${PROMETHEUS_REMOTE_WRITE_USER:-closet-pi}
      PROMETHEUS_REMOTE_WRITE_PASSWORD_BCRYPT: ${PROMETHEUS_REMOTE_WRITE_PASSWORD_BCRYPT:-}
      PROM_BASIC_AUTH_PASSWORD: ${PROM_BASIC_AUTH_PASSWORD:-}
```

- [ ] **Step 3: Swap env vars in .env.example**

In `.env.example`, delete the UniFi block (the comment + `UNIFI_PROTECT_HOST=…` + `UNIFI_PROTECT_API_KEY=`) and add:

```bash
# Prometheus remote-write basic auth (the Raspberry Pi closet collector pushes
# here). Leave the bcrypt blank to keep Prometheus open (no auth). Generate the
# hash with `htpasswd -nBC 10 closet-pi` and copy the part after the colon
# (see pi/README.md). PROM_BASIC_AUTH_PASSWORD is the plaintext Grafana sends;
# it must be the password that hashes to PROMETHEUS_REMOTE_WRITE_PASSWORD_BCRYPT.
PROMETHEUS_REMOTE_WRITE_USER=closet-pi
PROMETHEUS_REMOTE_WRITE_PASSWORD_BCRYPT=
PROM_BASIC_AUTH_PASSWORD=
```

- [ ] **Step 4: Swap Config entries in unraid-template.xml**

In `unraid-template.xml`, delete the two `<Config>` lines whose `Target` is `UNIFI_PROTECT_HOST` and `UNIFI_PROTECT_API_KEY`, and add three new ones (place them where the UniFi ones were):

```xml
  <Config Name="Prometheus Remote-Write User" Target="PROMETHEUS_REMOTE_WRITE_USER" Default="closet-pi" Mode="" Description="Basic-auth username the Raspberry Pi closet collector uses to remote_write into Prometheus. Must match REMOTE_WRITE_USER on the Pi." Type="Variable" Display="always" Required="false" Mask="false">closet-pi</Config>
  <Config Name="Prometheus Remote-Write Password (bcrypt)" Target="PROMETHEUS_REMOTE_WRITE_PASSWORD_BCRYPT" Default="" Mode="" Description="bcrypt hash of the remote-write password. Generate with: htpasswd -nBC 10 USER (use the hash part after the colon). Leave blank to keep Prometheus open (no auth)." Type="Variable" Display="always" Required="false" Mask="true"/>
  <Config Name="Prometheus Remote-Write Password (plain)" Target="PROM_BASIC_AUTH_PASSWORD" Default="" Mode="" Description="Plaintext of the same remote-write password (Grafana sends this to query the now-authenticated Prometheus). Must hash to the bcrypt above." Type="Variable" Display="always" Required="false" Mask="true"/>
```

- [ ] **Step 5: Validate the template XML**

Run: `python3 -c "import xml.dom.minidom as m; m.parse('unraid-template.xml'); print('OK')"`
Expected: `OK`

- [ ] **Step 6: Confirm the exporter script and its tests still exist (NOT removed)**

Run: `test -f rootfs/usr/local/bin/unifi-protect-exporter && test -f tests/test_unifi_protect_exporter.py && echo KEPT`
Expected: `KEPT`

- [ ] **Step 7: Run the existing exporter tests (regression — script unchanged)**

Run: `pytest tests/test_unifi_protect_exporter.py -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "Remove in-container UniFi exporter; add remote-write auth env vars"
```

---

## Task 5: Docs + Pi runbook

**Files:**
- Create: `pi/README.md`
- Modify: `README.md` (closet-environment section)

- [ ] **Step 1: Write the Pi runbook**

`pi/README.md`:

```markdown
# Closet collector (Raspberry Pi)

The Unraid container (192.168.1.100) cannot reach the UniFi Protect NVR
(192.168.0.159) — the 1.x→0.x direction is blocked. This Pi (192.168.0.95) sits
on the NVR's LAN, runs the UP-Sense exporter locally, and `remote_write`s the
metrics to the container's Prometheus.

## First-time setup

1. Clone this repo on the Pi.
2. `cp pi/.env.example pi/.env` and fill in:
   - `UNIFI_PROTECT_API_KEY` — a **freshly rotated** Protect Integration API key
     (Protect → Settings → Control Plane → Integrations). Do not reuse the key
     pasted in chat history; it is compromised.
   - `REMOTE_WRITE_USER` / `REMOTE_WRITE_PASSWORD` — must match the container's
     `PROMETHEUS_REMOTE_WRITE_USER` and the plaintext behind its bcrypt hash.
3. `docker compose -f pi/docker-compose.yml up -d --build`

## Generating the bcrypt hash (run once, on any machine with htpasswd)

```bash
htpasswd -nBC 10 closet-pi
# Output: closet-pi:$2b$10$....   <- copy the part AFTER the colon
```

Put the hash in the container's `PROMETHEUS_REMOTE_WRITE_PASSWORD_BCRYPT`
(Unraid template, masked) and the same plaintext in `PROM_BASIC_AUTH_PASSWORD`
and the Pi's `REMOTE_WRITE_PASSWORD`.

## Verify

```bash
# Exporter sees the sensor (run on the Pi):
docker exec unifi-protect-exporter \
  python3 -c "import urllib.request; print(urllib.request.urlopen('http://localhost:9688/metrics').read().decode())" \
  | grep Server\ Room

# Samples landed in Prometheus (run anywhere that can reach 192.168.1.100):
curl -s -u closet-pi:PASSWORD \
  'http://192.168.1.100:9090/api/v1/query?query=unifi_sensor_temperature_celsius' | jq .
```

## Updating

`git pull && docker compose -f pi/docker-compose.yml up -d --build`
```

- [ ] **Step 2: Update the main README closet section**

In `README.md`, find the closet-environment / UP-Sense description and update it to state the UP-Sense exporter now runs on the Raspberry Pi (192.168.0.95) and pushes via remote_write, because the NVR (192.168.0.159) is unreachable from the container. Point to `pi/README.md`. AC Infinity is unchanged (still in-container). Keep the edit to the existing section only — do not restructure the README.

- [ ] **Step 3: Commit**

```bash
git add pi/README.md README.md
git commit -m "Docs: Pi closet-collector runbook + README closet update"
```

---

## Deploy (after all tasks merge)

Auth is opt-in, so the safe sequence is:

1. **Ship the image** (push → CI → ghcr.io → ssh + `update_container`). With the bcrypt
   env still unset, Prometheus binds `0.0.0.0:9090` with **no** auth and the
   remote-write receiver on. Nothing breaks; all dashboards keep working.
2. **Pull the config-repo** on the host (`git pull` in
   `/mnt/user/appdb/unraidnotunhealthy/config-repo`) so the datasource carries
   the (blank-safe) creds, then `docker exec … s6-svc -r grafana`.
3. **Set the secrets** in the Unraid template: `PROMETHEUS_REMOTE_WRITE_USER`,
   `PROMETHEUS_REMOTE_WRITE_PASSWORD_BCRYPT`, `PROM_BASIC_AUTH_PASSWORD`, then
   Apply (recreates the container). Auth is now ON and Grafana is authenticated.
   - Reminder: template Config fields live in the **USB** template
     (`/boot/config/plugins/dockerMan/templates-user/my-UNRAIDnotUnHealthy.xml`),
     which does **not** sync from the repo `unraid-template.xml` — add the three
     fields there too (as done previously for the UniFi fields).
4. **Bring up the Pi stack** (`git pull && docker compose -f pi/docker-compose.yml up -d --build`
   on the Pi, with `pi/.env` filled in using the rotated key).
5. **Verify** per `pi/README.md` and confirm the existing system/disk/GPU
   dashboards still render (datasource auth working).

---

## Self-review notes

- **Spec coverage:** Pi exporter+vmagent (Task 1) ✓; remote-write receiver + 0.0.0.0 + basic auth + literal bcrypt write (Task 2) ✓; Grafana datasource creds (Task 3) ✓; remove in-container exporter + job + template env, keep script (Task 4) ✓; HTTP/LAN-trust (no TLS) ✓; failure-as-gap noted in spec, no alerting built (YAGNI) ✓; deploy ordering ✓; rotated key called out (Tasks 1, 5) ✓.
- **Refinement vs spec:** auth made *opt-in* (keyed on the bcrypt env) so step 1 of deploy can't blank dashboards — strictly safer than the spec's ordering, same end state.
- **Discovered detail not in spec:** the `prometheus` self-scrape job also needs creds once auth is on — handled via a `password_file` the init keeps in sync (Task 2 Step 8).
