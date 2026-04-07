# Kimi API Collector (Balance)

**File:** `app/services/collectors/kimi_api.py`

Moonshot AI (Kimi) balance collector with prepaid account tracking in USD ($).

> **Note:** This collector tracks API balance. For IDE coding quotas (weekly + rate limits), see [Kimi Coding Collector](kimi_coding.md).

---

## Overview

The Kimi Code collector retrieves prepaid account balance from Moonshot AI's Kimi models. Like zAI, Kimi uses a simple prepaid credit model where you add funds to your account and usage deducts from that balance.

### Key Features

- **Prepaid Balance Model**: Shows account balance ($) rather than usage quotas
- **Long Context**: Kimi K2.5 supports up to 2M token context window
- **Key Length Validation**: Checks key length >= 10 (minimum valid key)
- **Low Balance Warning**: Visual warning when balance drops below $5

---

## Data Source

### Primary: Moonshot AI Balance API

**Endpoint:** `https://api.moonshot.cn/v1/users/me/balance`

**Authentication:** Bearer token via `KIMI_API_KEY` environment variable

**Key Validation:**
- Checks key length >= 10 characters
- Returns error card if key missing or too short
- Distinguishes 401 Unauthorized from other errors

**Response Format:**
```json
{
  "data": {
    "available_balance": "45.75"
  }
}
```

**Model:** Prepaid credits in USD ($)

---

## Collection Flow

```mermaid
graph TD
    A[Start] --> B{KIMI_API_KEY set?}
    B -->|No| C[Return Error: Missing/Invalid Key]
    B -->|Yes| D{Key length >= 10?}
    D -->|No| C
    D -->|Yes| E[Call Moonshot API]
    
    E --> F{Status Code?}
    F -->|200| G[Parse Balance $]
    F -->|401| H[Return Error: Unauthorized]
    F -->|Other| I[Return Error: HTTP {code}]
    F -->|Timeout| J[Return Error: Connection Failed]
    
    G --> K[Return Balance Card]
    C --> L[Return Error Card]
    H --> L
    I --> L
    J --> L
```

---

## Output Format

### Standard Card

```python
{
    "service": "Kimi API",
    "icon": "🌙",
    "remaining": "$45.75",       # Available balance
    "unit": "balance",
    "reset": "Manual",           # Prepaid - no automatic reset
    "health": "good",            # > $5 = good
    "pace": "Stable",
    "detail": "Prepaid balance (API)",
}
```

### Error Card (Invalid Key)

```python
{
    "service": "Kimi API",
    "icon": "🌙",
    "remaining": "ERR",
    "unit": "Check State",
    "reset": "—",
    "health": "critical",
    "pace": "Stopped",
    "detail": "Missing/Invalid Key"
}
```

### Error Card (Unauthorized)

```python
{
    "service": "Kimi API",
    "icon": "🌙",
    "remaining": "ERR",
    "unit": "Check State",
    "reset": "—",
    "health": "critical",
    "pace": "Stopped",
    "detail": "Unauthorized"
}
```

---

## Health Calculation

Based on **account balance**:

```python
if balance > 5:
    health = "good"      # Green
else:
    health = "warning"   # Yellow (low balance)
```

**Threshold:** $5 USD

**Note:** No "critical" threshold - service simply stops when balance reaches zero.

---

## Configuration

### Environment Variables

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `KIMI_API_KEY` | Yes | Moonshot API key | `sk-proj-...` |

### Getting an API Key

1. Sign up at https://platform.moonshot.cn/
2. Navigate to "API Keys" (API密钥)
3. Create a new key (format: `sk-proj-xxx` or similar)
4. Add funds to your account via the billing page

### File Permissions

No special file permissions required - only needs the API key in environment.

---

## Error Handling

| Scenario | Behavior | User Action |
|----------|----------|-------------|
| Missing key | Error card | Set `KIMI_API_KEY` env var |
| Short key (< 10 chars) | Error card | Check key is complete |
| 401 Unauthorized | Error card | Key invalid or expired |
| Other HTTP error | Error card with code | Check API status |
| Connection timeout | Error card | Check network connectivity |

---

## Troubleshooting

### Issue: "Missing/Invalid Key" error

**Cause:** Environment variable not set or key too short

**Fix:**
```bash
export KIMI_API_KEY="sk-proj-your-key-here"
```

### Issue: "401 Unauthorized" error

**Cause:** Invalid or expired API key

**Fix:** 
1. Check key at https://platform.moonshot.cn/
2. Generate new key if needed
3. Key format should be `sk-proj-...` or similar

### Issue: Shows $0.00 balance

**Cause:** Account has no remaining credits

**Fix:** Add prepaid credits through Moonshot AI billing portal

### Issue: Connection Failed

**Cause:** Network issue or API endpoint down

**Check:**
```bash
curl -H "Authorization: Bearer $KIMI_API_KEY" \
  https://api.moonshot.cn/v1/users/me/balance
```

---

## Comparison: Prepaid vs Quota Models

| Aspect | Kimi Code (Prepaid) | Quota-Based (Claude, etc.) |
|--------|---------------------|---------------------------|
| **Metric** | Account balance ($) | Token/request quotas |
| **Reset** | Manual (add credits) | Automatic (time-based) |
| **Health** | Based on remaining $ | Based on % used |
| **Overage** | Stops working | May have extra usage tier |
| **Display** | "$45.75 balance" | "75% remaining" |

---

## Deployment Modes

### Standalone
Works directly with API key from environment. No local files needed.

### Multi-Host
Run sidecar on each machine with `KIMI_API_KEY` set. No aggregation needed since balance is account-wide.

### Docker
Set `KIMI_API_KEY` as environment variable in container. API key travels with container.

---

## Kimi Models

Kimi offers several models with different capabilities:

| Model | Context Window | Best For |
|-------|---------------|----------|
| **Kimi K2.5** | 2M tokens | Long documents, code analysis |
| **Kimi K2** | 200K tokens | General coding tasks |
| **Kimi Lite** | 128K tokens | Faster, cheaper inference |

All models share the same prepaid balance pool.

---

## Future Options

### Potential: Usage History API

**Current:** Only shows current balance

**Future:** Could query usage history if Moonshot provides endpoint:
- Daily/monthly spend tracking
- Model-specific usage breakdown
- Cost per 1K tokens

**Priority:** Low (balance is primary concern for prepaid model)

### Potential: Tier Detection

**Feature:** Show current pricing tier (if Moonshot introduces tiers)

**Priority:** Low (currently single tier)

---

## Related Files

| File | Purpose |
|------|---------|
| `app/services/collectors/kimi_api.py` | Main collector implementation |
| `app/core/config.py` | API key configuration |
| `tests/unit/test_collectors.py` | Unit tests (TestKimiApiCollector) |
| `scripts/sidecar.py` | Sidecar implementation |

---

## References

- **Moonshot AI Documentation:** https://platform.moonshot.cn/docs/
- **Kimi Models:** K2.5 series with long context support
- **API Console:** https://platform.moonshot.cn/

---

*Last updated: 2026-04-07*

## Troubleshooting

### Issue: "Missing/Invalid Key" error
**Cause:** `KIMI_API_KEY` not set or too short
**Fix:**
1. Get API key from https://platform.moonshot.cn/
2. Set `KIMI_API_KEY` in `.env`
3. Key must be at least 10 characters

### Issue: "401 Unauthorized"
**Cause:** Invalid or expired key
**Fix:**
1. Check key at https://platform.moonshot.cn/
2. Generate new key if needed
3. Ensure key is for API access (not IDE)

### Issue: Shows $0.00 balance
**Cause:** No credits remaining
**Fix:** Add prepaid credits via Moonshot billing portal

