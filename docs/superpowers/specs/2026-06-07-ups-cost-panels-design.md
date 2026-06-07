# UPS Running-Cost Panels — Design

**Date:** 2026-06-07
**Status:** Approved

## Goal

Show the electricity **cost** of the UPS-connected load (the Unraid server +
whatever else is on the UPS) on the existing **UPS / Power** dashboard, using the
operator's actual Alabama Power rate.

## Scope

- **In:** cost derived from the live `apcups_*` metrics already scraped. New
  panels + Grafana variables added to `grafana/dashboards/ups.json`. Dashboard-only.
- **Out:** whole-house cost (no live meter), time-of-use logic (Rate FD is not
  TOU), the fixed $14.50/mo base charge (not usage-driven), exporter/Prometheus
  changes.

## Rate model

Alabama Power **Rate FD** is seasonal + tiered, **not** time-of-use. The house
always exceeds the tier threshold, so the server's marginal kWh sit in Tier 2.
Per the user's decision, the dashboard uses a **single editable rate** defaulting
to the bill's all-in effective rate (**0.167 $/kWh**, includes ECR/NDR/tax),
exposed as a Grafana variable. No seasonal auto-switch.

## Power derivation

The UPS reports real-power load %. SMC1000 nominal real power = **600 W**.

    power_kW = apcups_load_percent / 100 * ups_nominal_w / 1000

`ups_nominal_w` is a second Grafana variable (default 600) so the assumption is
tweakable. Estimate accuracy ~±10% (depends on power factor / nominal).

## Grafana variables

| Name | Type | Default | Purpose |
|---|---|---|---|
| `rate` | textbox | `0.167` | $/kWh, all-in |
| `ups_nominal_w` | textbox | `600` | UPS nominal real-power watts |

Provisioned dashboard (`allowUiUpdates: false`): live edits apply for the
session; changing the saved default is a one-number JSON edit + `deploy-dashboards.sh`.

## Panels (new "Running Cost" row, appended to ups.json)

| Panel | Query (PromQL) | Unit |
|---|---|---|
| $/hour (now) | `apcups_load_percent/100*$ups_nominal_w/1000*$rate` | currencyUSD |
| Projected $/day | above `*24` | currencyUSD |
| Projected $/month | above `*730` | currencyUSD |
| Cost over range | `avg_over_time(apcups_load_percent[$__range])/100*$ups_nominal_w/1000*($__range_s/3600)*$rate` | currencyUSD |
| kWh over range | same without `*$rate` | kwatth |
| $/hour over time | `apcups_load_percent/100*$ups_nominal_w/1000*$rate` | currencyUSD |

## Deploy & verify

Dashboard-only → `scripts/deploy-dashboards.sh` (push + host config-repo pull +
~30s hot-reload; no rebuild/recreate). Verify: dashboard reloads, variables
present, $/hr panel shows a sane non-zero value.
