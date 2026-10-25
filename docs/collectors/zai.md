# zAI Collector

**File:** `app/services/collectors/zai.py`

Zhipu AI (GLM) **quota** collector. It reads the token and time limit of a
zAI coding plan and reports them as a Runway card.

> **Quota only.** This collector has no prepaid-balance endpoint. An earlier
> version of this documentation described a balance collector for
> `open.bigmodel.cn/api/paas/v4/users/me/balance`; that endpoint was never
> part of this collector and does not exist in the codebase. The collector's own
> docstring records the same fact: *"CodexBar only uses quota endpoint, no
> balance."*

## Overview

One server-side collector, one strategy, one card. The zAI coding plan exposes
two limit kinds — a token allowance and a time allowance — and the collector
reports the one it finds first, as a `variant` on the same card.

## Setup Methods Quick Overview

| Method | Where | Notes |
|---|---|---|
| API key (server-side) | Settings → Providers, or `ZAI_API_KEY` in `.env` | The registry declares exactly one rule: the `ZAI_API_KEY` environment variable. |

There is no sidecar strategy for zAI. The sidecar may carry the key if it was
entered on the host, but every zAI card is produced server-side.

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

The token comes from the configured provider entry first and from
`ZAI_API_KEY` second.

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
| `ZAI_API_KEY` | — | The API key. Also the only registry rule for `zai`. |
| `ZAI_API_HOST` | `api.z.ai` | Override the host. |
| `ZAI_QUOTA_URL` | — | Override the full quota URL. |

## Sidecar Support

None. zAI is collected entirely server-side. The sidecar may forward the key
if it was configured on the host, but it never emits a zAI card.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| No card at all | No key resolved. Check the provider entry, then `ZAI_API_KEY`. |
| `remaining` is `0` | The quota endpoint answered but reported nothing remaining — usually an exhausted plan rather than an auth failure. |
| `reset` shows `Manual` | The API did not return a reset timestamp. |
| The China region is unreachable | Set `ZAI_API_HOST=open.bigmodel.cn`, or `ZAI_QUOTA_URL` to the full endpoint. |
| `health` is `unknown` | The response carried no usable percentage. |

## Related Files

| File | Purpose |
|---|---|
| `app/services/collectors/zai.py` | Collector entry point — quota only |
| `app/services/collectors/base.py` | Collector base class |
| `scripts/sidecar.py` | Sidecar credential rules and event dispatch |

## References

- **zAI:** https://z.ai
- **Zhipu AI (China):** https://open.bigmodel.cn
