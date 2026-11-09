# zAI Collector

**File:** `app/services/collectors/zai.py`

Zhipu AI (GLM) **quota** collector. It reads the token and time limit of a
zAI coding plan and reports them as a Runway card.

This collector reports quota only; zAI has no prepaid-balance endpoint.

## Overview

One server-side collector, one strategy, one card. The zAI coding plan exposes
two limit kinds — a token allowance and a time allowance — and the collector
reports the one it finds first, as a `variant` on the same card.

## Credentials

The key comes from Settings, Providers, then `ZAI_API_KEY`. The registry has one rule, the `ZAI_API_KEY` environment variable, which a sidecar can also discover (with a key-scoped origin, see [architecture.md](../architecture.md#credential-origins-and-operator-tags)); every zAI card is still produced server-side.

## Data Sources

The registry id is `zai`; the display name is **zAI API**.

| Source | Strategy | Endpoint |
|---|---|---|
| `api` | server | `GET https://api.z.ai/api/monitor/usage/quota/limit` |
| `api` | server | `GET https://open.bigmodel.cn/api/monitor/usage/quota/limit` (China fallback) |

The two endpoints are tried in that order. Two environment variables override
them:

| Variable | Effect |
|---|---|
| `ZAI_API_HOST` | Override the host outright, e.g. `open.bigmodel.cn` for the China region. |
| `ZAI_QUOTA_URL` | Override the full quota URL. |

## Output Format

The card is emitted with `service_name: "zAI"` and a `variant` that says which
limit kind it is. The plan name is carried inside `detail`, not in a `tier`
field — zAI cards have **no `tier` and no `usage_url`**.

| Limit kind | `variant` | `unit_type` | `unit` | `detail` |
|---|---|---|---|---|
| `TOKENS_LIMIT` | `Tokens` | `tokens` | the token allowance | `<used> used · <plan name>` |
| `TIME_LIMIT` | `Time` | `minutes` | `<n> min` | `<n> min used · <plan name>` |
| anything else | the raw limit type | `generic` | the raw number | `<used> used · <plan name>` |

```json
{
  "service_name": "zAI",
  "variant": "Tokens",
  "icon": "🌐",
  "remaining": "750,000",
  "unit": "1,000,000",
  "reset": "in 12 days",
  "health": "good",
  "pace": "Stable",
  "detail": "250,000 used · Pro",
  "used_value": 250000.0,
  "limit_value": 1000000.0,
  "is_unlimited": false,
  "unit_type": "tokens",
  "data_source": "api"
}
```

`reset` is `human_delta(reset_dt)` — a relative phrase such as `in 12 days` —
when the API returns a reset timestamp, `"Manual"` when it does not, and
`"Unknown"` when the timestamp cannot be parsed. `reset_at` carries the ISO
timestamp whenever one is available.

`health` comes from `HealthCalculator.from_percentage(pct_used)`, so it is one
of `good` / `warning` / `critical` / `unknown`. `pace` is derived from
`health`: `Stable`, `High` or `Critical`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ZAI_API_KEY` | — | The API key. |
| `ZAI_API_HOST` | `api.z.ai` | Override the host. |
| `ZAI_QUOTA_URL` | — | Override the full quota URL. |

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| No card at all | No key resolved. Check the provider entry, then `ZAI_API_KEY`. |
| `remaining` is `0` | The quota endpoint answered but reported nothing remaining — usually an exhausted plan rather than an auth failure. |
| `reset` shows `Manual` | The API did not return a reset timestamp. |
| The China region is unreachable | Set `ZAI_API_HOST=open.bigmodel.cn`, or `ZAI_QUOTA_URL` to the full endpoint. |
| `health` is `unknown` | The response carried no usable percentage. |

## Related files

| File | Purpose |
|---|---|
| `app/services/collectors/zai.py` | Collector entry point — quota only |
| `scripts/sidecar.py` | Sidecar credential rules and event dispatch |
