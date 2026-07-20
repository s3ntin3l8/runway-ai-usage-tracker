"""
Mock data for AI provider API responses.
Used in tests and fixtures.
"""

ANTHROPIC_OAUTH_RESPONSE = {
    "five_hour": {"utilization": 45.5, "resets_at": "2025-04-07T12:00:00Z"},
    "seven_day": {"utilization": 62.3, "resets_at": "2025-04-14T00:00:00Z"},
}

# Newer response shape: a top-level `limits` array of self-describing window objects
# (kind/group/percent/severity/scope) that COEXISTS with the legacy dict-keyed-by-
# window-name shape above — a hybrid body, not a replacement. Captured live from
# claude.ai/api/organizations/{org_id}/usage (the web scraper endpoint); the OAuth
# API (api.anthropic.com/api/oauth/usage) is presumed to have migrated identically.
# All legacy per-model keys (seven_day_opus, seven_day_sonnet, ...) are null — only
# five_hour/seven_day duplicate limits[] session/weekly_all (same values: 25/89).
ANTHROPIC_OAUTH_LIMITS_RESPONSE = {
    "five_hour": {
        "utilization": 25.0,
        "resets_at": "2026-07-20T21:30:00.333553+00:00",
        "limit_dollars": None,
        "used_dollars": None,
        "remaining_dollars": None,
    },
    "seven_day": {
        "utilization": 89.0,
        "resets_at": "2026-07-21T18:00:00.333599+00:00",
        "limit_dollars": None,
        "used_dollars": None,
        "remaining_dollars": None,
    },
    "seven_day_opus": None,
    "seven_day_sonnet": None,
    "seven_day_oauth_apps": None,
    "seven_day_cowork": None,
    "seven_day_omelette": None,
    "tangelo": None,
    "iguana_necktie": None,
    "omelette_promotional": None,
    "nimbus_quill": None,
    "cinder_cove": None,
    "amber_ladder": None,
    "extra_usage": {
        "is_enabled": False,
        "monthly_limit": None,
        "used_credits": None,
        "utilization": None,
        "currency": None,
        "decimal_places": None,
        "disabled_reason": None,
        "daily": None,
        "weekly": None,
    },
    "spend": {
        "used": {"amount_minor": 0, "currency": "USD", "exponent": 2},
        "limit": None,
        "percent": 0,
        "severity": "normal",
        "enabled": False,
        "disabled_reason": None,
        "cap": None,
        "balance": None,
        "auto_reload": None,
        "can_purchase_credits": True,
        "can_toggle": True,
    },
    "limits": [
        {
            "kind": "session",
            "group": "session",
            "percent": 25,
            "severity": "normal",
            "resets_at": "2026-07-20T21:30:00.333553+00:00",
            "scope": None,
            "is_active": False,
        },
        {
            "kind": "weekly_all",
            "group": "weekly",
            "percent": 89,
            "severity": "warning",
            "resets_at": "2026-07-21T18:00:00.333599+00:00",
            "scope": None,
            "is_active": True,
        },
        {
            "kind": "weekly_scoped",
            "group": "weekly",
            "percent": 0,
            "severity": "normal",
            "resets_at": None,
            "scope": {
                "model": {"id": None, "display_name": "Fable"},
                "surface": None,
            },
            "is_active": False,
        },
    ],
}

CLAUDE_WEB_API_ORGS_RESPONSE = [{"uuid": "org_test_123", "id": "org_test_123", "name": "Test Org"}]

CLAUDE_WEB_API_USAGE_RESPONSE = {
    "five_hour": {"utilization": 0.455, "resets_at": "2025-04-07T12:00:00Z"},
    "seven_day": {"utilization": 0.623, "resets_at": "2025-04-14T00:00:00Z"},
}

GEMINI_QUOTA_RESPONSE = {
    "buckets": [
        {
            "modelId": "gemini-2.0-flash",
            "remainingFraction": 0.75,
            "resetTime": "2025-04-08T00:00:00Z",
        },
        {
            "modelId": "gemini-1.5-pro",
            "remainingFraction": 0.82,
            "resetTime": "2025-04-08T00:00:00Z",
        },
    ]
}

GITHUB_COPILOT_RESPONSE = {
    "limited_user_quotas": {"completions": 45, "chat": 120},
    "limited_user_reset_date": "2025-04-08T00:00:00Z",
    "quota_snapshots": [
        {"metric": "premium_interactions", "remaining": 450, "entitlement": 500},
        {"metric": "chat", "remaining": 890, "entitlement": 1000},
    ],
    "copilot_plan": "Pro",
}

CHATGPT_USAGE_RESPONSE = {
    "plan_type": "free",
    "rate_limit": {
        "primary_window": {
            "used_percent": 55.3,
            "reset_at": 1744876800,  # Unix timestamp
        }
    },
}

OPENCODE_GO_RESPONSE = {"total_usage_usd": 12.50, "hard_limit_usd": 50.00}

BIGMODEL_ZAI_RESPONSE = {"data": {"available_balance": 125.45}}

KIMI_RESPONSE = {"data": {"available_balance": 8.75}}
