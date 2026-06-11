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
   - `UNIFI_PROTECT_HOST` defaults to `192.168.0.159` (the NVR) — only change it
     if your NVR has a different address.
   - `REMOTE_WRITE_USER` defaults to `closet-pi`. Leave it as-is: the container's
     Prometheus web-config hardcodes that username, so changing it here would
     break auth. `REMOTE_WRITE_PASSWORD` must be the plaintext behind the
     container's `PROMETHEUS_REMOTE_WRITE_PASSWORD_BCRYPT` (i.e. equal to the
     container's `PROM_BASIC_AUTH_PASSWORD`).
3. From the repo root: `cd pi && docker compose up -d --build`
   (cd into `pi/` so Compose loads `pi/.env` on every Compose version; the build
   context `..` still resolves to the repo root.)

## Generating the bcrypt hash (run once, on any machine with htpasswd)

```bash
htpasswd -nBC 10 closet-pi
# Output: closet-pi:$2b$10$....   <- copy the part AFTER the colon
```

Put the hash in the container's `PROMETHEUS_REMOTE_WRITE_PASSWORD_BCRYPT`
(Unraid template, masked) and the same plaintext in `PROM_BASIC_AUTH_PASSWORD`
(container) and `REMOTE_WRITE_PASSWORD` (Pi `.env`).

## Verify

```bash
# Exporter sees the sensor (run on the Pi):
docker exec unifi-protect-exporter \
  python3 -c "import urllib.request; print(urllib.request.urlopen('http://localhost:9688/metrics').read().decode())" \
  | grep "Server Room"

# Samples landed in Prometheus (run anywhere that can reach 192.168.1.100):
curl -s -u closet-pi:PASSWORD \
  'http://192.168.1.100:9090/api/v1/query?query=unifi_sensor_temperature_celsius' | jq .
```

## Updating

```bash
git pull && cd pi && docker compose up -d --build
```
