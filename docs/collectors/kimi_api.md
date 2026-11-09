# Kimi API Collector (Balance)

**File:** `app/services/collectors/kimi_api.py`

Moonshot AI (Kimi) prepaid balance collector in USD ($).

## Overview

- **Strategy:** `api`, `data_source: api`.
- **Cards:** one (account balance).

## Credentials

`KIMI_API_KEY` in the environment or Settings (Moonshot key, at least 10 characters). A sidecar can also discover the environment variable, with a key-scoped origin ([architecture.md](../architecture.md#credential-origins-and-operator-tags)); the balance is fetched server-side.

## Data Source

### Primary: Moonshot AI Balance API
**Endpoint:** `https://api.moonshot.ai/v1/users/me/balance`
**Auth:** Bearer token
**Key:** at least 10 characters

## Card

One USD balance card (`unit_type: currency`, `data_source: api`, reset "Manual").

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `KIMI_API_KEY` | Yes | Moonshot API key (min 10 chars) |

## Troubleshooting

### "Missing/Invalid Key" error
**Fix:**
1. Get key from https://platform.moonshot.cn/
2. `export KIMI_API_KEY="sk-proj-..."` (must be ≥10 chars)

### "401 Unauthorized"
**Fix:** Check key at https://platform.moonshot.cn/ and regenerate if needed

### Shows $0.00 balance
**Cause:** No credits remaining
**Fix:** Add prepaid credits via Moonshot billing portal

## Related files

| File | Purpose |
|---|---|
| `app/services/collectors/kimi_api.py` | Collector entry point |
| `scripts/sidecar.py` | Sidecar credential rules and event dispatch |

For IDE coding quotas see [Kimi Coding](kimi_coding.md).
