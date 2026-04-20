
import asyncio
from app.services.collector_manager import manager
from app.core.registry import registry
from sqlmodel import Session, select
from app.core.db import engine
from app.models.db import ProviderConfig

async def check_chatgpt():
    p_id = "chatgpt"
    provider_def = registry.get_provider(p_id) or {}
    rules = provider_def.get("rules", [])
    
    supports_api_key = any(
        any(k in rule.get("mapping", {}).values() for k in ("api_key", "oauth_token"))
        for rule in rules
        if rule.get("type") in ("env", "file", "keychain")
    )
    supports_session_cookie = any(
        "session_cookie" in rule.get("mapping", {}).values()
        for rule in rules
        if rule.get("type") in ("env", "file", "keychain")
    )
    
    print(f"Provider: {p_id}")
    print(f"Supports API Key: {supports_api_key}")
    print(f"Supports Session Cookie: {supports_session_cookie}")
    print(f"Rules count: {len(rules)}")

if __name__ == "__main__":
    asyncio.run(check_chatgpt())
