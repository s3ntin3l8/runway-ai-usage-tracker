"""
Mock data for AI provider API responses.
Used in tests and fixtures.
"""

ANTHROPIC_OAUTH_RESPONSE = {
    "five_hour": {"utilization": 45.5, "resets_at": "2025-04-07T12:00:00Z"},
    "seven_day": {"utilization": 62.3, "resets_at": "2025-04-14T00:00:00Z"},
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
