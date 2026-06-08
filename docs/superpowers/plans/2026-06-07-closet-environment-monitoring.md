# Closet Environment Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two stdlib-Python Prometheus exporters — UniFi Protect UP-Sense and AC Infinity Controller 69 Pro — and surface them as a "Closet Environment" row on the existing UPS/Power dashboard.

**Architecture:** Each exporter follows the proven `ups-modbus-exporter` pattern: a background thread polls the upstream on an interval and caches a snapshot under a lock; the HTTP `/metrics` handler serves the cached snapshot (never blocks a scrape). On any failure the service stays alive and reports `<prefix>_scrape_success=0` (idle-visible). Stdlib only — no new runtime dependencies.

**Tech Stack:** Python 3 stdlib (`http.server`, `urllib`, `ssl`, `json`, `threading`), s6-overlay longrun services, Prometheus, Grafana, Docker.

**Design ref:** `docs/superpowers/specs/2026-06-07-closet-environment-monitoring-design.md`

---

## File Structure

**New files:**
- `rootfs/usr/local/bin/unifi-protect-exporter` — UP-Sense exporter (mode 100755)
- `rootfs/usr/local/bin/acinfinity-exporter` — AC Infinity exporter (mode 100755)
- `rootfs/etc/s6-overlay/s6-rc.d/unifi-protect-exporter/{run,type,dependencies.d/base}`
- `rootfs/etc/s6-overlay/s6-rc.d/acinfinity-exporter/{run,type,dependencies.d/base}`
- `rootfs/etc/s6-overlay/s6-rc.d/user/contents.d/unifi-protect-exporter` (empty marker)
- `rootfs/etc/s6-overlay/s6-rc.d/user/contents.d/acinfinity-exporter` (empty marker)
- `tests/test_unifi_protect_exporter.py`
- `tests/test_acinfinity_exporter.py`

**Modified files:**
- `rootfs/etc/prometheus/prometheus.yml` — two scrape jobs
- `.env.example` — credential placeholders
- `docker-compose.yml` — env passthrough for local dev
- `unraid-template.xml` — `<Config>` entries (production source of truth)
- `grafana/dashboards/ups.json` — "Closet Environment" row

**Test loading note:** the exporter scripts have no `.py` extension. Tests load them via `importlib.util.spec_from_file_location`; because the module name is not `"__main__"`, the `main()` guard does not run, so importing only defines the pure `decode_*` functions. All module-level `os.environ.get(...)` calls have defaults, so import is side-effect-free.

---

## Task 1: UniFi Protect UP-Sense exporter

This exporter is built first because it is verifiable against the live NVR right now (Protect 7.1.76 at `192.168.0.159`). The decode test uses a **real captured** API response.

**Files:**
- Create: `tests/test_unifi_protect_exporter.py`
- Create: `rootfs/usr/local/bin/unifi-protect-exporter`
- Create: `rootfs/etc/s6-overlay/s6-rc.d/unifi-protect-exporter/run`
- Create: `rootfs/etc/s6-overlay/s6-rc.d/unifi-protect-exporter/type`
- Create: `rootfs/etc/s6-overlay/s6-rc.d/unifi-protect-exporter/dependencies.d/base`
- Create: `rootfs/etc/s6-overlay/s6-rc.d/user/contents.d/unifi-protect-exporter`
- Modify: `rootfs/etc/prometheus/prometheus.yml`
- Modify: `.env.example`, `docker-compose.yml`, `unraid-template.xml`

- [ ] **Step 1: Write the failing test**

Create `tests/test_unifi_protect_exporter.py`. The fixture is a trimmed but real `/sensors` response (two sensors: one full UP-Sense, one with null light/humidity).

```python
import importlib.util
import pathlib
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "rootfs/usr/local/bin/unifi-protect-exporter"


def load_module():
    spec = importlib.util.spec_from_file_location("unifi_protect_exporter", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SAMPLE = [
    {
        "id": "68cbf99d02732c03e443099b",
        "modelKey": "sensor",
        "state": "CONNECTED",
        "name": "Server Room",
        "batteryStatus": {"percentage": 95, "isLow": False},
        "stats": {
            "light": {"value": 25, "status": "neutral"},
            "humidity": {"value": 44, "status": "neutral"},
            "temperature": {"value": 26.7, "status": "neutral"},
        },
        "wirelessConnectionState": {"signalState": {"signalQuality": 88, "signalStrength": -66}},
    },
    {
        "id": "68868e9f01b1cc03e4001c11",
        "modelKey": "sensor",
        "state": "CONNECTED",
        "name": "Front Door",
        "batteryStatus": {"percentage": 85, "isLow": False},
        "stats": {
            "light": {"value": None, "status": "unknown"},
            "humidity": {"value": None, "status": "unknown"},
            "temperature": {"value": 22.58, "status": "neutral"},
        },
        "wirelessConnectionState": {"signalState": {"signalQuality": 80, "signalStrength": -58}},
    },
]


class DecodeSensors(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()
        self.samples = self.mod.decode_sensors(SAMPLE)

    def _find(self, metric, name):
        return [s for s in self.samples if s["metric"] == metric and s["labels"]["name"] == name]

    def test_temperature_celsius_emitted_for_both(self):
        self.assertEqual(self._find("unifi_sensor_temperature_celsius", "Server Room")[0]["value"], 26.7)
        self.assertEqual(self._find("unifi_sensor_temperature_celsius", "Front Door")[0]["value"], 22.58)

    def test_humidity_skipped_when_null(self):
        self.assertEqual(len(self._find("unifi_sensor_humidity_percent", "Server Room")), 1)
        self.assertEqual(len(self._find("unifi_sensor_humidity_percent", "Front Door")), 0)

    def test_light_skipped_when_null(self):
        self.assertEqual(self._find("unifi_sensor_light_lux", "Server Room")[0]["value"], 25.0)
        self.assertEqual(len(self._find("unifi_sensor_light_lux", "Front Door")), 0)

    def test_battery_and_signal(self):
        self.assertEqual(self._find("unifi_sensor_battery_percent", "Server Room")[0]["value"], 95.0)
        self.assertEqual(self._find("unifi_sensor_signal_dbm", "Server Room")[0]["value"], -66.0)

    def test_connected_flag(self):
        self.assertEqual(self._find("unifi_sensor_connected", "Server Room")[0]["value"], 1.0)

    def test_empty_payload_is_empty(self):
        self.assertEqual(self.mod.decode_sensors([]), [])
        self.assertEqual(self.mod.decode_sensors(None), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_unifi_protect_exporter -v`
Expected: FAIL — `FileNotFoundError` / `spec is None` because the script does not exist yet.

- [ ] **Step 3: Create the exporter script**

Create `rootfs/usr/local/bin/unifi-protect-exporter`:

```python
#!/usr/bin/env python3
"""unifi-protect-exporter — poll UniFi Protect's official Integration API for
UP-Sense environmental sensors and expose them as Prometheus metrics.

Endpoint (UniFi OS 4.x / Protect 5.x+, read-only):
  GET https://<UNIFI_PROTECT_HOST>/proxy/protect/integration/v1/sensors
  header: X-API-KEY: <UNIFI_PROTECT_API_KEY>
The NVR serves a self-signed cert, so TLS verification is disabled (local LAN).

A background thread polls on an interval and caches the decoded samples under a
lock; the HTTP handler serves the cached snapshot so a scrape never blocks on
the NVR. On any failure (or unset config) the service stays alive and reports
unifi_sensor_scrape_success=0 -- the idle-visible pattern shared with
ups-modbus-exporter and bgw-nat-scraper.

All five sensors are exported, labeled by name; the dashboard filters to the
closet ("Server Room"). null stat values are skipped (not emitted).

Stdlib only.
"""
import http.server
import json
import os
import socketserver
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request

HOST = os.environ.get("UNIFI_PROTECT_HOST", "").strip()
API_KEY = os.environ.get("UNIFI_PROTECT_API_KEY", "").strip()
LISTEN_ADDR = os.environ.get("UNIFI_PROTECT_EXPORTER_ADDR", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("UNIFI_PROTECT_EXPORTER_PORT", "9688"))
POLL_INTERVAL = int(os.environ.get("UNIFI_PROTECT_POLL_INTERVAL", "60"))
TIMEOUT = int(os.environ.get("UNIFI_PROTECT_TIMEOUT", "8"))

PREFIX = "unifi_sensor"

_state = {"last_success": 0.0, "duration": 0.0, "ok": False, "err": "", "samples": []}
_lock = threading.Lock()

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

_HELP = {
    f"{PREFIX}_temperature_celsius": "UP-Sense temperature (Celsius)",
    f"{PREFIX}_humidity_percent": "UP-Sense relative humidity (percent)",
    f"{PREFIX}_light_lux": "UP-Sense ambient light level",
    f"{PREFIX}_battery_percent": "UP-Sense battery charge (percent)",
    f"{PREFIX}_signal_dbm": "UP-Sense wireless signal strength (dBm)",
    f"{PREFIX}_connected": "1 if the sensor state is CONNECTED",
}


def log(msg):
    print(f"unifi-protect-exporter: {msg}", file=sys.stderr, flush=True)


def _fetch():
    url = f"https://{HOST}/proxy/protect/integration/v1/sensors"
    req = urllib.request.Request(
        url, headers={"X-API-KEY": API_KEY, "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=_SSL_CTX) as resp:
        return json.loads(resp.read().decode())


def decode_sensors(payload):
    """payload: list of sensor objects from the Integration API.

    Returns a list of {"metric", "labels", "value"} dicts. null stats are
    skipped so the dashboard shows a gap rather than a fake 0.
    """
    samples = []
    for s in payload or []:
        name = s.get("name") or s.get("id") or "unknown"
        labels = {"name": name}
        stats = s.get("stats") or {}

        def stat(key):
            return (stats.get(key) or {}).get("value")

        t = stat("temperature")
        if t is not None:
            samples.append({"metric": f"{PREFIX}_temperature_celsius", "labels": labels, "value": float(t)})
        h = stat("humidity")
        if h is not None:
            samples.append({"metric": f"{PREFIX}_humidity_percent", "labels": labels, "value": float(h)})
        light = stat("light")
        if light is not None:
            samples.append({"metric": f"{PREFIX}_light_lux", "labels": labels, "value": float(light)})
        bat = (s.get("batteryStatus") or {}).get("percentage")
        if bat is not None:
            samples.append({"metric": f"{PREFIX}_battery_percent", "labels": labels, "value": float(bat)})
        sig = (((s.get("wirelessConnectionState") or {}).get("signalState")) or {}).get("signalStrength")
        if sig is not None:
            samples.append({"metric": f"{PREFIX}_signal_dbm", "labels": labels, "value": float(sig)})
        samples.append({
            "metric": f"{PREFIX}_connected",
            "labels": labels,
            "value": 1.0 if s.get("state") == "CONNECTED" else 0.0,
        })
    return samples


def _scrape_loop():
    while True:
        t0 = time.time()
        try:
            if not HOST or not API_KEY:
                raise RuntimeError("UNIFI_PROTECT_HOST/API_KEY not set")
            samples = decode_sensors(_fetch())
            with _lock:
                _state["samples"] = samples
                _state["ok"] = True
                _state["err"] = ""
                _state["last_success"] = time.time()
                _state["duration"] = time.time() - t0
        except Exception as e:  # noqa: BLE001 - stay alive, surface via scrape_success
            log(f"scrape failed: {e}")
            with _lock:
                _state["ok"] = False
                _state["err"] = str(e)[:200]
                _state["duration"] = time.time() - t0
        time.sleep(POLL_INTERVAL)


def _escape(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


def _fmt_labels(labels):
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items()) + "}"


def _render_metrics():
    with _lock:
        s = dict(_state)
        samples = list(_state["samples"])
    age = time.time() - s["last_success"] if s["last_success"] else -1.0
    lines = [
        "# HELP unifi_sensor_scrape_success 1 if the last Protect API poll succeeded",
        "# TYPE unifi_sensor_scrape_success gauge",
        f"unifi_sensor_scrape_success {1 if s['ok'] else 0}",
        "# HELP unifi_sensor_scrape_duration_seconds Wall time of the last poll",
        "# TYPE unifi_sensor_scrape_duration_seconds gauge",
        f"unifi_sensor_scrape_duration_seconds {s['duration']:.6f}",
        "# HELP unifi_sensor_scrape_age_seconds Seconds since last successful poll (-1 if never)",
        "# TYPE unifi_sensor_scrape_age_seconds gauge",
        f"unifi_sensor_scrape_age_seconds {age:.3f}",
    ]
    emitted = set()
    for samp in samples:
        m = samp["metric"]
        if m not in emitted:
            if m in _HELP:
                lines.append(f"# HELP {m} {_HELP[m]}")
            lines.append(f"# TYPE {m} gauge")
            emitted.add(m)
        lines.append(f"{m}{_fmt_labels(samp['labels'])} {samp['value']:.4f}")
    return ("\n".join(lines) + "\n").encode()


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        body = _render_metrics()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args, **_kw):
        pass


class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    if not HOST or not API_KEY:
        log("UNIFI_PROTECT_HOST/API_KEY not set -- exporter idles, scrape_success=0")
    threading.Thread(target=_scrape_loop, daemon=True).start()
    srv = _ThreadingServer((LISTEN_ADDR, LISTEN_PORT), _Handler)
    log(f"listening on {LISTEN_ADDR}:{LISTEN_PORT}, polling {HOST or '(unset)'} every {POLL_INTERVAL}s")
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Then make it executable:

Run: `chmod +x rootfs/usr/local/bin/unifi-protect-exporter`

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_unifi_protect_exporter -v`
Expected: PASS (6 tests OK).

- [ ] **Step 5: Live smoke test against the NVR (optional but recommended)**

Run (replace the key with the real one from your env — do NOT commit it):
```bash
UNIFI_PROTECT_HOST=192.168.0.159 UNIFI_PROTECT_API_KEY=<key> \
  python3 rootfs/usr/local/bin/unifi-protect-exporter &
sleep 3 && curl -s 127.0.0.1:9688/metrics | grep -E 'unifi_sensor_(scrape_success|temperature)' ; kill %1
```
Expected: `unifi_sensor_scrape_success 1` and a `unifi_sensor_temperature_celsius{name="Server Room"} 2X.XXXX` line.

- [ ] **Step 6: Create the s6 longrun service**

Create `rootfs/etc/s6-overlay/s6-rc.d/unifi-protect-exporter/type` with exact contents:
```
longrun
```

Create `rootfs/etc/s6-overlay/s6-rc.d/unifi-protect-exporter/run` (will be chmod +x'd by the Dockerfile's `find ... -name run` rule; also set it now for git):
```bash
#!/command/with-contenv bash
# unifi-protect-exporter — poll the UniFi Protect Integration API for UP-Sense
# environmental sensors and expose them on 127.0.0.1:9688. Configure the NVR via
# UNIFI_PROTECT_HOST + UNIFI_PROTECT_API_KEY. Read-only; idles with
# unifi_sensor_scrape_success=0 when unset or unreachable.
exec /usr/local/bin/unifi-protect-exporter
```

Create the empty dependency marker `rootfs/etc/s6-overlay/s6-rc.d/unifi-protect-exporter/dependencies.d/base` (empty file).

Create the empty service-enable marker `rootfs/etc/s6-overlay/s6-rc.d/user/contents.d/unifi-protect-exporter` (empty file).

Run:
```bash
chmod +x rootfs/etc/s6-overlay/s6-rc.d/unifi-protect-exporter/run
mkdir -p rootfs/etc/s6-overlay/s6-rc.d/unifi-protect-exporter/dependencies.d
touch rootfs/etc/s6-overlay/s6-rc.d/unifi-protect-exporter/dependencies.d/base
touch rootfs/etc/s6-overlay/s6-rc.d/user/contents.d/unifi-protect-exporter
```

- [ ] **Step 7: Add the Prometheus scrape job**

In `rootfs/etc/prometheus/prometheus.yml`, append after the `ups_modbus` job block (before the `# ---- Active DNS health probes` comment):

```yaml
  # UniFi Protect UP-Sense environmental sensors. The unifi-protect-exporter
  # sidecar polls the NVR's Integration API and exposes temp / humidity / light
  # / battery per sensor. Reports unifi_sensor_scrape_success=0 when the NVR is
  # unreachable or the API key is unset, so the closet panels show a gap.
  - job_name: unifi_protect
    scrape_interval: 60s
    static_configs:
      - targets: ['127.0.0.1:9688']
```

- [ ] **Step 8: Add config (env + compose + template)**

In `.env.example`, append before the `# Optional overrides` block:
```bash
# UniFi Protect UP-Sense exporter — leave UNIFI_PROTECT_API_KEY blank to skip.
# Create a read-only API key in Protect: Settings → Control Plane → Integrations.
UNIFI_PROTECT_HOST=192.168.0.159
UNIFI_PROTECT_API_KEY=
```

In `docker-compose.yml`, inside `environment:`, after the Radarr lines:
```yaml
      # UniFi Protect UP-Sense exporter — leave the key unset to disable.
      UNIFI_PROTECT_HOST: ${UNIFI_PROTECT_HOST:-}
      UNIFI_PROTECT_API_KEY: ${UNIFI_PROTECT_API_KEY:-}
```

In `unraid-template.xml`, after the `UPS_MODBUS_HOST` `<Config>` line (and any matching `<Environment>`/`<Config>` block for it), add:
```xml
  <Config Name="UniFi Protect Host" Target="UNIFI_PROTECT_HOST" Default="" Mode="" Description="LAN IP/hostname of the UniFi Protect NVR (the console the UP-Sense sensors pair to). The unifi-protect-exporter polls its Integration API for closet temp/humidity/light/battery (UPS / Power dashboard). Leave UNIFI_PROTECT_API_KEY blank to disable." Type="Variable" Display="always" Required="false" Mask="false"/>
  <Config Name="UniFi Protect API Key" Target="UNIFI_PROTECT_API_KEY" Default="" Mode="" Description="Read-only API key from Protect → Settings → Control Plane → Integrations. Leave blank to disable the UP-Sense exporter and its dashboard panels." Type="Variable" Display="always" Required="false" Mask="true"/>
```

- [ ] **Step 9: Commit**

```bash
chmod +x rootfs/usr/local/bin/unifi-protect-exporter rootfs/etc/s6-overlay/s6-rc.d/unifi-protect-exporter/run
git add rootfs/usr/local/bin/unifi-protect-exporter \
        rootfs/etc/s6-overlay/s6-rc.d/unifi-protect-exporter \
        rootfs/etc/s6-overlay/s6-rc.d/user/contents.d/unifi-protect-exporter \
        rootfs/etc/prometheus/prometheus.yml .env.example docker-compose.yml \
        unraid-template.xml tests/test_unifi_protect_exporter.py
git commit -m "Add UniFi Protect UP-Sense exporter"
```

---

## Task 2: AC Infinity Controller 69 Pro exporter

The AC Infinity cloud response schema is from community reverse-engineering (homebridge-acinfinity API reference), not a live capture — the user just bought the controller. The decode test therefore uses a **synthetic** fixture matching the documented schema, and Step 5 captures the live response to reconcile field names/units before relying on it.

**Files:**
- Create: `tests/test_acinfinity_exporter.py`
- Create: `rootfs/usr/local/bin/acinfinity-exporter`
- Create: `rootfs/etc/s6-overlay/s6-rc.d/acinfinity-exporter/{run,type,dependencies.d/base}`
- Create: `rootfs/etc/s6-overlay/s6-rc.d/user/contents.d/acinfinity-exporter`
- Modify: `rootfs/etc/prometheus/prometheus.yml`, `.env.example`, `docker-compose.yml`, `unraid-template.xml`

- [ ] **Step 1: Write the failing test**

Create `tests/test_acinfinity_exporter.py`:

```python
import importlib.util
import pathlib
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "rootfs/usr/local/bin/acinfinity-exporter"


def load_module():
    spec = importlib.util.spec_from_file_location("acinfinity_exporter", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Synthetic fixture matching the documented devInfoListAll schema. Values are
# x100 for temperature/humidity/vpd; port "speak" is the 0-10 current power.
SAMPLE = [
    {
        "devName": "Closet",
        "devId": "12345",
        "deviceInfo": {
            "temperature": 2670,
            "humidity": 4400,
            "vpdnums": 152,
            "ports": [
                {"port": 1, "portName": "Exhaust Fan", "speak": 6, "online": 1},
                {"port": 2, "portName": "Empty", "speak": 0, "online": 0},
            ],
        },
    }
]


class DecodeDevices(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()
        self.samples = self.mod.decode_devices(SAMPLE)

    def _find(self, metric, **labels):
        out = []
        for s in self.samples:
            if s["metric"] != metric:
                continue
            if all(s["labels"].get(k) == v for k, v in labels.items()):
                out.append(s)
        return out

    def test_temperature_scaled_to_celsius(self):
        self.assertAlmostEqual(self._find("acinfinity_temperature_celsius", device="Closet")[0]["value"], 26.70)

    def test_humidity_scaled(self):
        self.assertAlmostEqual(self._find("acinfinity_humidity_percent", device="Closet")[0]["value"], 44.00)

    def test_vpd_scaled(self):
        self.assertAlmostEqual(self._find("acinfinity_vpd_kpa", device="Closet")[0]["value"], 1.52)

    def test_fan_power_per_port(self):
        self.assertEqual(self._find("acinfinity_fan_power", device="Closet", port="1")[0]["value"], 6.0)
        self.assertEqual(self._find("acinfinity_fan_power", device="Closet", port="2")[0]["value"], 0.0)

    def test_port_online(self):
        self.assertEqual(self._find("acinfinity_port_online", device="Closet", port="1")[0]["value"], 1.0)

    def test_empty_payload_is_empty(self):
        self.assertEqual(self.mod.decode_devices([]), [])
        self.assertEqual(self.mod.decode_devices(None), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_acinfinity_exporter -v`
Expected: FAIL — script does not exist yet.

- [ ] **Step 3: Create the exporter script**

Create `rootfs/usr/local/bin/acinfinity-exporter`:

```python
#!/usr/bin/env python3
"""acinfinity-exporter — poll the AC Infinity cloud API for a WiFi-connected
UIS controller (Controller 69 Pro) and expose climate + fan metrics.

AC Infinity has no official public API; this uses the community-documented
cloud endpoints (homebridge-acinfinity API reference):
  POST /api/user/appUserLogin        appEmail + appPasswordl -> appId token
  POST /api/user/devInfoListAll      userId param, token header -> device list
Base URL is plain HTTP (no TLS) -- credentials traverse the network
unencrypted, so use a dedicated AC Infinity account.

A background thread logs in, polls on an interval, and caches decoded samples;
the HTTP handler serves the cached snapshot. On any failure (or unset creds) the
service stays alive and reports acinfinity_scrape_success=0 (idle-visible).

Raw temperature/humidity/vpd fields are x100; fan "speak" is the 0-10 current
power level. The controller's display unit must be set to Celsius (the API
returns the displayed unit); see ACINFINITY_TEMP_UNIT to convert if set to F.

Stdlib only.
"""
import http.server
import json
import os
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("ACINFINITY_BASE_URL", "http://www.acinfinityserver.com").rstrip("/")
EMAIL = os.environ.get("ACINFINITY_EMAIL", "").strip()
PASSWORD = os.environ.get("ACINFINITY_PASSWORD", "")
TEMP_UNIT = os.environ.get("ACINFINITY_TEMP_UNIT", "c").strip().lower()
LISTEN_ADDR = os.environ.get("ACINFINITY_EXPORTER_ADDR", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("ACINFINITY_EXPORTER_PORT", "9687"))
POLL_INTERVAL = int(os.environ.get("ACINFINITY_POLL_INTERVAL", "60"))
TIMEOUT = int(os.environ.get("ACINFINITY_TIMEOUT", "8"))

# A browser-ish UA; the API rejects some default urllib agents.
_UA = "ACController/1.8.2 (com.acinfinity.humiture; build:489; iOS 16.0)"

_state = {"last_success": 0.0, "duration": 0.0, "ok": False, "err": "", "samples": []}
_lock = threading.Lock()
_token = None

_HELP = {
    "acinfinity_temperature_celsius": "Controller probe temperature (Celsius)",
    "acinfinity_humidity_percent": "Controller probe relative humidity (percent)",
    "acinfinity_vpd_kpa": "Vapor pressure deficit (kPa)",
    "acinfinity_fan_power": "Port current power level (0-10)",
    "acinfinity_port_online": "1 if a device is detected on the port",
}


def log(msg):
    print(f"acinfinity-exporter: {msg}", file=sys.stderr, flush=True)


def _post(path, fields, token=None):
    data = urllib.parse.urlencode(fields).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": _UA}
    if token:
        headers["token"] = token
    req = urllib.request.Request(BASE + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        body = json.loads(resp.read().decode())
    if body.get("code") != 200:
        raise RuntimeError(f"{path} failed: code={body.get('code')} msg={body.get('msg')}")
    return body.get("data")


def _login():
    data = _post("/api/user/appUserLogin", {"appEmail": EMAIL, "appPasswordl": PASSWORD[:25]})
    # appId doubles as both the auth token and the userId for subsequent calls.
    return data["appId"]


def _to_celsius(raw_x100):
    c = raw_x100 / 100.0
    if TEMP_UNIT == "f":
        return (c - 32.0) * 5.0 / 9.0
    return c


def decode_devices(data):
    """data: list of device objects from devInfoListAll.

    Returns a list of {"metric", "labels", "value"} dicts. Field names follow
    the community-documented schema; reconcile against a live capture before
    relying on production values (see plan Task 2 Step 5).
    """
    samples = []
    for dev in data or []:
        dname = dev.get("devName") or str(dev.get("devId", "unknown"))
        info = dev.get("deviceInfo") or {}
        dlabels = {"device": dname}
        if info.get("temperature") is not None:
            samples.append({"metric": "acinfinity_temperature_celsius", "labels": dlabels, "value": _to_celsius(info["temperature"])})
        if info.get("humidity") is not None:
            samples.append({"metric": "acinfinity_humidity_percent", "labels": dlabels, "value": info["humidity"] / 100.0})
        if info.get("vpdnums") is not None:
            samples.append({"metric": "acinfinity_vpd_kpa", "labels": dlabels, "value": info["vpdnums"] / 100.0})
        for p in info.get("ports") or []:
            plabels = {"device": dname, "port": str(p.get("port", "")), "port_name": p.get("portName", "")}
            if p.get("speak") is not None:
                samples.append({"metric": "acinfinity_fan_power", "labels": plabels, "value": float(p["speak"])})
            if p.get("online") is not None:
                samples.append({"metric": "acinfinity_port_online", "labels": plabels, "value": float(p["online"])})
    return samples


def _scrape_loop():
    global _token
    while True:
        t0 = time.time()
        try:
            if not EMAIL or not PASSWORD:
                raise RuntimeError("ACINFINITY_EMAIL/PASSWORD not set")
            if _token is None:
                _token = _login()
            try:
                data = _post("/api/user/devInfoListAll", {"userId": _token}, token=_token)
            except (urllib.error.HTTPError, RuntimeError):
                _token = _login()  # token likely expired -> relogin once
                data = _post("/api/user/devInfoListAll", {"userId": _token}, token=_token)
            samples = decode_devices(data)
            with _lock:
                _state["samples"] = samples
                _state["ok"] = True
                _state["err"] = ""
                _state["last_success"] = time.time()
                _state["duration"] = time.time() - t0
        except Exception as e:  # noqa: BLE001 - stay alive, surface via scrape_success
            log(f"scrape failed: {e}")
            _token = None
            with _lock:
                _state["ok"] = False
                _state["err"] = str(e)[:200]
                _state["duration"] = time.time() - t0
        time.sleep(POLL_INTERVAL)


def _escape(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


def _fmt_labels(labels):
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items()) + "}"


def _render_metrics():
    with _lock:
        s = dict(_state)
        samples = list(_state["samples"])
    age = time.time() - s["last_success"] if s["last_success"] else -1.0
    lines = [
        "# HELP acinfinity_scrape_success 1 if the last cloud API poll succeeded",
        "# TYPE acinfinity_scrape_success gauge",
        f"acinfinity_scrape_success {1 if s['ok'] else 0}",
        "# HELP acinfinity_scrape_duration_seconds Wall time of the last poll",
        "# TYPE acinfinity_scrape_duration_seconds gauge",
        f"acinfinity_scrape_duration_seconds {s['duration']:.6f}",
        "# HELP acinfinity_scrape_age_seconds Seconds since last successful poll (-1 if never)",
        "# TYPE acinfinity_scrape_age_seconds gauge",
        f"acinfinity_scrape_age_seconds {age:.3f}",
    ]
    emitted = set()
    for samp in samples:
        m = samp["metric"]
        if m not in emitted:
            if m in _HELP:
                lines.append(f"# HELP {m} {_HELP[m]}")
            lines.append(f"# TYPE {m} gauge")
            emitted.add(m)
        lines.append(f"{m}{_fmt_labels(samp['labels'])} {samp['value']:.4f}")
    return ("\n".join(lines) + "\n").encode()


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        body = _render_metrics()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args, **_kw):
        pass


class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    if not EMAIL or not PASSWORD:
        log("ACINFINITY_EMAIL/PASSWORD not set -- exporter idles, scrape_success=0")
    threading.Thread(target=_scrape_loop, daemon=True).start()
    srv = _ThreadingServer((LISTEN_ADDR, LISTEN_PORT), _Handler)
    log(f"listening on {LISTEN_ADDR}:{LISTEN_PORT}, polling {BASE} every {POLL_INTERVAL}s")
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Run: `chmod +x rootfs/usr/local/bin/acinfinity-exporter`

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_acinfinity_exporter -v`
Expected: PASS (6 tests OK).

- [ ] **Step 5: Reconcile against a live capture (do once the controller is on WiFi)**

Once the Controller 69 Pro is connected to WiFi in the AC Infinity app, capture the real response and confirm field names/units match the decode:
```bash
ACINFINITY_EMAIL=<email> ACINFINITY_PASSWORD=<pw> \
  python3 rootfs/usr/local/bin/acinfinity-exporter &
sleep 4 && curl -s 127.0.0.1:9687/metrics | grep -E 'acinfinity_(scrape_success|temperature|fan_power)'; kill %1
```
Expected: `acinfinity_scrape_success 1` plus temperature/fan_power lines.
If fields differ (e.g. `devName` vs `deviceName`, temperature unit, nesting), adjust `decode_devices` and update the fixture in `tests/test_acinfinity_exporter.py` to match the captured shape, then re-run Step 4. If the controller is not yet on WiFi, skip this step — the exporter idles harmlessly with `scrape_success=0` until creds + device are live.

- [ ] **Step 6: Create the s6 longrun service**

Create `rootfs/etc/s6-overlay/s6-rc.d/acinfinity-exporter/type`:
```
longrun
```

Create `rootfs/etc/s6-overlay/s6-rc.d/acinfinity-exporter/run`:
```bash
#!/command/with-contenv bash
# acinfinity-exporter — poll the AC Infinity cloud API for a WiFi-connected UIS
# controller and expose climate/fan metrics on 127.0.0.1:9687. Configure
# ACINFINITY_EMAIL + ACINFINITY_PASSWORD. Idles with acinfinity_scrape_success=0
# when unset or the controller is offline.
exec /usr/local/bin/acinfinity-exporter
```

Run:
```bash
chmod +x rootfs/etc/s6-overlay/s6-rc.d/acinfinity-exporter/run
mkdir -p rootfs/etc/s6-overlay/s6-rc.d/acinfinity-exporter/dependencies.d
touch rootfs/etc/s6-overlay/s6-rc.d/acinfinity-exporter/dependencies.d/base
touch rootfs/etc/s6-overlay/s6-rc.d/user/contents.d/acinfinity-exporter
```

- [ ] **Step 7: Add the Prometheus scrape job**

In `rootfs/etc/prometheus/prometheus.yml`, append immediately after the `unifi_protect` job added in Task 1:
```yaml
  # AC Infinity Controller 69 Pro (WiFi -> cloud). The acinfinity-exporter
  # sidecar logs into the AC Infinity cloud API and exposes probe temp /
  # humidity / VPD and per-port fan power. Reports acinfinity_scrape_success=0
  # when creds are unset or the controller is offline.
  - job_name: acinfinity
    scrape_interval: 60s
    static_configs:
      - targets: ['127.0.0.1:9687']
```

- [ ] **Step 8: Add config (env + compose + template)**

In `.env.example`, append after the UniFi Protect block from Task 1:
```bash
# AC Infinity Controller 69 Pro (cloud API) — leave ACINFINITY_PASSWORD blank to
# skip. NOTE: AC Infinity's API is plain HTTP (no TLS); use a dedicated account.
ACINFINITY_EMAIL=
ACINFINITY_PASSWORD=
```

In `docker-compose.yml`, inside `environment:`, after the UniFi Protect lines:
```yaml
      # AC Infinity cloud exporter — leave the password unset to disable.
      ACINFINITY_EMAIL: ${ACINFINITY_EMAIL:-}
      ACINFINITY_PASSWORD: ${ACINFINITY_PASSWORD:-}
```

In `unraid-template.xml`, after the UniFi Protect `<Config>` lines from Task 1:
```xml
  <Config Name="AC Infinity Email" Target="ACINFINITY_EMAIL" Default="" Mode="" Description="Email for your AC Infinity app account. The acinfinity-exporter logs into the AC Infinity cloud API to read closet temp/humidity/VPD and fan power for a WiFi-connected Controller 69 Pro. NOTE: AC Infinity's API is plain HTTP (no TLS) — use a dedicated account. Leave the password blank to disable." Type="Variable" Display="always" Required="false" Mask="false"/>
  <Config Name="AC Infinity Password" Target="ACINFINITY_PASSWORD" Default="" Mode="" Description="Password for your AC Infinity app account (sent over plain HTTP — use a dedicated account). Leave blank to disable the AC Infinity exporter and its dashboard panels." Type="Variable" Display="always" Required="false" Mask="true"/>
```

- [ ] **Step 9: Commit**

```bash
chmod +x rootfs/usr/local/bin/acinfinity-exporter rootfs/etc/s6-overlay/s6-rc.d/acinfinity-exporter/run
git add rootfs/usr/local/bin/acinfinity-exporter \
        rootfs/etc/s6-overlay/s6-rc.d/acinfinity-exporter \
        rootfs/etc/s6-overlay/s6-rc.d/user/contents.d/acinfinity-exporter \
        rootfs/etc/prometheus/prometheus.yml .env.example docker-compose.yml \
        unraid-template.xml tests/test_acinfinity_exporter.py
git commit -m "Add AC Infinity Controller 69 Pro exporter"
```

---

## Task 3: "Closet Environment" dashboard row

Append a new row + panels to `grafana/dashboards/ups.json`. Existing dashboard: schemaVersion 39, uid `unhealthy-ups`, 19 panels, max panel id 26, content ends at y=30. New panels start at y=31 (the row header at y=31, panels below).

**Files:**
- Modify: `grafana/dashboards/ups.json`

- [ ] **Step 1: Add the panels via a Python script**

Because hand-editing a 19-panel JSON is error-prone, use this script to append the row. Run it from the repo root:

```python
import json

path = "grafana/dashboards/ups.json"
d = json.load(open(path))
panels = d["panels"]
base_id = max(p.get("id", 0) for p in panels)  # 26

PROM = {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}
# Match the datasource uid actually used by existing panels.
existing_ds = None
for p in panels:
    ds = p.get("datasource")
    if isinstance(ds, dict) and ds.get("type") == "prometheus":
        existing_ds = ds
        break
if existing_ds:
    PROM = existing_ds

CLOSET = 'name="Server Room"'

def ts(pid, title, x, y, w, h, exprs, unit):
    return {
        "id": pid, "type": "timeseries", "title": title, "datasource": PROM,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "fieldConfig": {"defaults": {"unit": unit, "custom": {"drawStyle": "line", "lineInterpolation": "smooth", "fillOpacity": 10}}, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
        "targets": [{"refId": chr(65 + i), "datasource": PROM, "expr": e, "legendFormat": lf} for i, (e, lf) in enumerate(exprs)],
    }

def stat(pid, title, x, y, w, h, expr, unit, legend="{{name}}"):
    return {
        "id": pid, "type": "stat", "title": title, "datasource": PROM,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "fieldConfig": {"defaults": {"unit": unit, "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}]}}, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value", "graphMode": "area"},
        "targets": [{"refId": "A", "datasource": PROM, "expr": expr, "legendFormat": legend}],
    }

row = {"id": base_id + 1, "type": "row", "title": "Closet Environment",
       "collapsed": False, "gridPos": {"h": 1, "w": 24, "x": 0, "y": 31}, "panels": []}

new = [
    row,
    ts(base_id + 2, "Temperature", 0, 32, 12, 8,
       [(f'acinfinity_temperature_celsius', "AC Infinity {{device}}"),
        (f'unifi_sensor_temperature_celsius{{{CLOSET}}}', "UP-Sense {{name}}")], "celsius"),
    ts(base_id + 3, "Humidity", 12, 32, 12, 8,
       [(f'acinfinity_humidity_percent', "AC Infinity {{device}}"),
        (f'unifi_sensor_humidity_percent{{{CLOSET}}}', "UP-Sense {{name}}")], "percent"),
    ts(base_id + 4, "VPD", 0, 40, 8, 8,
       [(f'acinfinity_vpd_kpa', "{{device}}")], "pressurekpa"),
    ts(base_id + 5, "Fan Power (per port)", 8, 40, 8, 8,
       [(f'acinfinity_fan_power', "{{device}} p{{port}} {{port_name}}")], "short"),
    stat(base_id + 6, "UP-Sense Battery", 16, 40, 4, 8, f'unifi_sensor_battery_percent{{{CLOSET}}}', "percent"),
    stat(base_id + 7, "UP-Sense Signal", 20, 40, 4, 8, f'unifi_sensor_signal_dbm{{{CLOSET}}}', "dBm"),
    stat(base_id + 8, "AC Infinity Link", 0, 48, 12, 4, "acinfinity_scrape_success", "bool_on_off", legend=""),
    stat(base_id + 9, "UP-Sense Link", 12, 48, 12, 4, "unifi_sensor_scrape_success", "bool_on_off", legend=""),
]

panels.extend(new)
json.dump(d, open(path, "w"), indent=2)
print("appended", len(new), "panels; new max id", base_id + 9)
```

Save this as `scripts/_add_closet_row.py`, run `python3 scripts/_add_closet_row.py`, then delete it (`rm scripts/_add_closet_row.py`) — it is a one-shot generator, not a committed tool.

- [ ] **Step 2: Validate the JSON**

Run: `python3 -c "import json; d=json.load(open('grafana/dashboards/ups.json')); print('panels', len(d['panels']), 'ids unique', len(set(p['id'] for p in d['panels']))==len(d['panels']))"`
Expected: `panels 28 ids unique True`

- [ ] **Step 3: Visual check (optional, if iterating locally)**

Run `docker compose up -d --build`, open Grafana `http://localhost:3000`, navigate to the UPS / Power dashboard, confirm the "Closet Environment" row renders. With no live creds the timeseries show "No data" and the link tiles show OFF — expected.

- [ ] **Step 4: Commit**

```bash
git add grafana/dashboards/ups.json
git commit -m "Add Closet Environment row to UPS/Power dashboard"
```

---

## Task 4: Build verification + README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Full unit-test run**

Run: `python3 -m unittest discover -s tests -v`
Expected: all tests from both exporters PASS.

- [ ] **Step 2: Build the image and check the new services start**

Run: `docker compose up -d --build`
Then: `docker exec unraidnotunhealthy sh -c 's6-rc -a list | grep -E "acinfinity|unifi-protect"'`
Expected: both `acinfinity-exporter` and `unifi-protect-exporter` listed as up.

Then verify both exporters serve metrics (idle is fine — `scrape_success 0` with no creds):
```bash
docker exec unraidnotunhealthy sh -c 'wget -qO- 127.0.0.1:9687/metrics | grep acinfinity_scrape_success; wget -qO- 127.0.0.1:9688/metrics | grep unifi_sensor_scrape_success'
```
Expected: one line each (`acinfinity_scrape_success 0`, `unifi_sensor_scrape_success 0` when creds unset).

- [ ] **Step 3: Confirm Prometheus picked up both jobs**

Run: `docker exec unraidnotunhealthy sh -c 'wget -qO- "127.0.0.1:9090/api/v1/targets" | grep -oE "\"job\":\"(acinfinity|unifi_protect)\""'`
Expected: both job names appear.

- [ ] **Step 4: Update README**

In `README.md`, find the exporter/services list (where `ups-modbus-exporter` and `bgw-nat-scraper` are documented) and add matching one-line entries:
```
- **acinfinity-exporter** (`:9687`) — AC Infinity Controller 69 Pro closet climate + fan power via the AC Infinity cloud API. Set `ACINFINITY_EMAIL`/`ACINFINITY_PASSWORD`.
- **unifi-protect-exporter** (`:9688`) — UniFi Protect UP-Sense temp/humidity/light/battery via the NVR Integration API. Set `UNIFI_PROTECT_HOST`/`UNIFI_PROTECT_API_KEY`.
```
(Match the exact surrounding format — bullet style, code-span conventions — of the existing exporter entries.)

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "Document closet environment exporters in README"
```

- [ ] **Step 6: Deploy**

Use the `deploy` skill (push → CI builds ghcr.io image → ssh + update_container). Remember the production env vars (`UNIFI_PROTECT_HOST`, `UNIFI_PROTECT_API_KEY`, `ACINFINITY_EMAIL`, `ACINFINITY_PASSWORD`) must be set on the Unraid template (template XML on the USB is the source of truth) — the committed template only carries blank placeholders. After deploy, rotate the Protect API key that was shared in chat.

---

## Self-Review Notes

- **Spec coverage:** Both exporters (cloud HTTP / local HTTPS), all named metrics, scrape_success idle-visible, secrets-as-placeholders, dashboard row with both temperature sources, security caveats — each maps to a task above. ✓
- **Type consistency:** `decode_sensors` (Protect) and `decode_devices` (AC Infinity) return the same `{"metric","labels","value"}` sample shape consumed by the identical `_render_metrics`/`_fmt_labels` helpers in each script. Metric names match between exporter, prometheus job, and dashboard exprs. ✓
- **Known soft spot:** AC Infinity field schema is documented, not live-captured — Task 2 Step 5 reconciles it explicitly and the exporter idles safely until then.
