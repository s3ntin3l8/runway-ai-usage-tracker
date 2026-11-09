# Kimi K2 Collector (Credits)

**File:** `app/services/collectors/kimi_k2.py`

Kimi K2 (Credits) collector with credit balance tracking for the Kimi coding agent product.

## Overview

- **Strategy:** `api` (REST), `data_source: api`.
- **Cards:** one (credit balance).

## Credentials

`KIMI_K2_API_KEY` in the environment or Settings (at least 10 characters). A sidecar can also discover the environment variable, with a key-scoped origin ([architecture.md](../architecture.md#credential-origins-and-operator-tags)); the balance is fetched server-side.

## Data Source

### Primary: Kimi K2 Credits API
**Endpoint:** `https://kimi-k2.ai/api/user/credits`
**Auth:** Bearer token
**Key Format:** Minimum 10 characters

## Card

One credits card (`unit_type: credits`, `data_source: api`, reset "Manual"); the detail shows credits consumed.

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `KIMI_K2_API_KEY` | Yes | Kimi K2 API key (min 10 chars) |

## Troubleshooting

### "Missing/Invalid Key" error
**Fix:**
1. Ensure `KIMI_K2_API_KEY` is set in your environment.
2. The key must be at least 10 characters long.

### "Unauthorized"
**Fix:** Verify the API key is valid and has permissions to access the credits endpoint.

## Related files

| File | Purpose |
|---|---|
| `app/services/collectors/kimi_k2.py` | Collector entry point |
| `scripts/sidecar.py` | Sidecar credential rules and event dispatch |

## References

- **Kimi K2:** https://kimi-k2.ai
- **Kimi Coding IDE:** [Kimi Coding Collector](kimi_coding.md)
- **Moonshot API:** [Kimi API Collector](kimi_api.md)
