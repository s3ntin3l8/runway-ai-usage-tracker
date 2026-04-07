"""
Runway — AI Subscription Limits Dashboard
FastAPI backend: collects live data from 8+ sources.
"""
from __future__ import annotations
import os
import json
import asyncio
import glob
import subprocess
import httpx
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Runway — AI Limits Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

class PaceCalculator:
    @staticmethod
    def estimate_longevity(pct_used: float, reset_at: datetime | None) -> str:
        if pct_used <= 0: return "Stable"
        if not reset_at: return "Sustainable"
        now = datetime.now(timezone.utc)
        if reset_at.tzinfo is None: reset_at = reset_at.replace(tzinfo=timezone.utc)
        time_to_reset = (reset_at - now).total_seconds()
        if time_to_reset <= 0: return "Pending Reset"
        remaining_pct = 100 - pct_used
        if remaining_pct <= 0: return "Exhausted"
        if remaining_pct < 10: return "Fast Burn"
        if remaining_pct < 30: return "Moderate Burn"
        return "Sustainable"

def _human_delta(target_dt: datetime | None) -> str:
    if not target_dt: return "—"
    now = datetime.now(timezone.utc)
    if target_dt.tzinfo is None: target_dt = target_dt.replace(tzinfo=timezone.utc)
    diff = target_dt - now
    seconds = int(diff.total_seconds())
    if seconds < 0: return "Just now"
    if seconds < 60: return f"{seconds}s"
    if seconds < 3600: return f"{seconds // 60}m"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"

def _error_card(service: str, icon: str, message: str):
    return {
        "service": service,
        "icon": icon,
        "remaining": "ERR",
        "unit": "Check State",
        "reset": "—",
        "health": "critical",
        "pace": "Stopped",
        "detail": message
    }

# ─────────────────────────────────────────────────────────────────────────────
# COLLECTORS
# ─────────────────────────────────────────────────────────────────────────────

async def get_claude_oauth(client: httpx.AsyncClient, token: str):
    url = "https://api.anthropic.com/api/oauth/usage"
    headers = {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"}
    try:
        resp = await client.get(url, headers=headers, timeout=10.0)
        if resp.status_code == 401: return [_error_card("Claude Pro", "🟠", "Unauthorized (OAuth)")]
        if resp.status_code != 200: return [_error_card("Claude Pro", "🟠", f"API Error {resp.status_code}")]
        
        data = resp.json()
        results = []
        # The API returns a dictionary of usage types (e.g., 'five_hour', 'seven_day')
        # Each has 'utilization' (0.0 to 100.0) and 'resets_at'
        for key, usage in data.items():
            if not isinstance(usage, dict) or "utilization" not in usage:
                continue
            
            u_type = key.replace("_", " ").title()
            pct_used = usage.get("utilization", 0.0)
            remaining_pct = 100.0 - pct_used
            
            reset_raw = usage.get("resets_at") or usage.get("resetsAt")
            reset_at = None
            if reset_raw:
                try:
                    reset_at = datetime.fromisoformat(reset_raw.replace("Z", "+00:00"))
                except:
                    pass
            
            results.append({
                "service": f"Claude ({u_type})",
                "icon": "🟠",
                "remaining": f"{remaining_pct:.1f}%",
                "unit": "capacity",
                "reset": _human_delta(reset_at),
                "health": "good" if pct_used < 70 else "warning" if pct_used < 90 else "critical",
                "pace": PaceCalculator.estimate_longevity(pct_used, reset_at),
                "detail": f"{pct_used:.1f}% of quota used [OAuth]",
            })
        return results if results else [_error_card("Claude Pro", "🟠", "No quota data")]
    except Exception as e: 
        return [_error_card("Claude Pro", "🟠", f"Connection Fail: {str(e)[:20]}")]

async def get_claude_local():
    projects_dir = os.path.expanduser("~/.claude/projects")
    limit = 2000000
    try:
        files = glob.glob(f"{projects_dir}/**/*.jsonl", recursive=True)
        if not files: return None # Silence if path not used
        cutoff = datetime.now(timezone.utc) - timedelta(hours=5)
        total_tokens = 0
        oldest: datetime | None = None
        for fpath in files:
            with open(fpath, "r", encoding="utf-8") as f:
                for line in f:
                    entry = json.loads(line)
                    if entry.get("type") != "assistant": continue
                    ts = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
                    if ts < cutoff: continue
                    usage = entry.get("message", {}).get("usage", {})
                    total_tokens += (usage.get("input_tokens", 0) + usage.get("output_tokens", 0))
                    if not oldest or ts < oldest: oldest = ts
        remaining = max(0, limit - total_tokens)
        pct = (total_tokens / limit * 100) if limit > 0 else 0
        reset_at = (oldest + timedelta(hours=5)) if oldest else None
        return [{
            "service": "Claude Pro",
            "icon": "🟠",
            "remaining": f"{remaining:,}",
            "unit": "tokens / 5h",
            "reset": _human_delta(reset_at),
            "health": "good" if pct < 70 else "warning" if pct < 90 else "critical",
            "pace": PaceCalculator.estimate_longevity(pct, reset_at),
            "detail": f"{total_tokens:,} / {limit:,} [Logs]",
        }]
    except: return None

async def get_gemini_cli():
    sessions_dir = os.path.expanduser("~/.gemini/tmp/sessions")
    try:
        files = glob.glob(f"{sessions_dir}/*.jsonl")
        if not files: return None
        total = 0
        for fpath in files:
            with open(fpath, "r") as f:
                for line in f:
                    u = json.loads(line).get("usage", {})
                    total += (u.get("prompt_tokens", 0) + u.get("completion_tokens", 0))
        return [{
            "service": "Gemini CLI",
            "icon": "🔵",
            "remaining": f"{total:,}",
            "unit": "tokens (24h)",
            "reset": "Rolling 24h",
            "health": "good",
            "pace": "Stable",
            "detail": "Local session logs",
        }]
    except: return None

async def get_github(client: httpx.AsyncClient):
    token = os.getenv("GITHUB_TOKEN")
    if not token: return []
    try:
        # Use Copilot internal endpoints for detailed metrics
        # Authorization: token <token> (note: 'token' prefix vs 'Bearer')
        # X-GitHub-Api-Version: 2025-04-01
        headers = {
            "Authorization": f"token {token}",
            "X-GitHub-Api-Version": "2025-04-01",
            "Accept": "application/json"
        }
        
        # 1. Fetch Copilot Token Info
        token_resp = await client.get("https://api.github.com/copilot_internal/v2/token", headers=headers)
        
        # 2. Fetch User/Quota Info
        user_resp = await client.get("https://api.github.com/copilot_internal/user", headers=headers)

        # 3. Fetch Billing Info (often holds the "True" limits for Individual accounts)
        billing_resp = await client.get("https://api.github.com/user/billing/subscription", headers={"Authorization": f"Bearer {token}"})
        
        cards = []
        plan_type = "Individual"
        if billing_resp.status_code == 200:
            bdata = billing_resp.json()
            plan_type = bdata.get("plan", {}).get("name", "Individual").title()
        
        if token_resp.status_code == 200:
            token_data = token_resp.json()
            # Handle free/trial limited usage
            if "limited_user_quotas" in token_data:
                quotas = token_data["limited_user_quotas"]
                reset_date = token_data.get("limited_user_reset_date")
                reset_at = None
                if reset_date:
                    try: reset_at = datetime.fromisoformat(reset_date.replace("Z", "+00:00"))
                    except: pass
                
                for key in ["completions", "chat"]:
                    if key in quotas:
                        val = quotas[key]
                        # For free/limited, it's usually "remaining"
                        cards.append({
                            "service": f"Copilot ({key.title()})",
                            "icon": "🐙",
                            "remaining": f"{val:,}",
                            "unit": "remaining",
                            "reset": _human_delta(reset_at),
                            "health": "good" if val > 10 else "warning",
                            "pace": "Manual",
                            "detail": f"{val} requests left [Internal]",
                        })

        if user_resp.status_code == 200:
            user_data = user_resp.json()
            snapshots = user_data.get("quota_snapshots", [])
            plan = user_data.get("copilot_plan", "Individual")
            
            for snap in snapshots:
                metric_raw = snap.get("metric", "unknown")
                metric = metric_raw.replace("_", " ").title()
                rem = snap.get("remaining")
                ent = snap.get("entitlement")
                
                if rem is not None and ent is not None:
                    pct = (ent - rem) / ent * 100 if ent > 0 else 0
                    cards.append({
                        "service": f"Copilot ({metric})",
                        "icon": "🐙",
                        "remaining": f"{rem:,}",
                        "unit": f"/ {ent:,}",
                        "reset": "Rolling",
                        "health": "good" if (rem/ent) > 0.3 else "warning" if (rem/ent) > 0.1 else "critical",
                        "pace": "Sustainable",
                        "detail": f"{pct:.1f}% used • {plan} [Snapshot]",
                    })
            
            # If no detailed snapshots but we have user info, show the base plan status
            if not cards:
                reset_date = user_data.get("quota_reset_date")
                reset_at = None
                if reset_date:
                    try: reset_at = datetime.fromisoformat(reset_date.replace("Z", "+00:00"))
                    except: pass

                cards.append({
                    "service": "GitHub Copilot",
                    "icon": "🐙",
                    "remaining": "Active",
                    "unit": plan.title(),
                    "reset": _human_delta(reset_at) if reset_at else "Monthly",
                    "health": "good",
                    "pace": "Stable",
                    "detail": f"Subscription active [Individual]",
                })
        
        # Fallback to standard rate limit if no specific copilot data found
        if not cards:
            resp = await client.get("https://api.github.com/rate_limit", headers={"Authorization": f"Bearer {token}"})
            if resp.status_code == 200:
                data = resp.json()["resources"]["core"]
                rem, lim = data["remaining"], data["limit"]
                reset_at = datetime.fromtimestamp(data["reset"], tz=timezone.utc)
                cards.append({
                    "service": "GitHub API",
                    "icon": "🐙",
                    "remaining": f"{rem:,}",
                    "unit": "requests",
                    "reset": _human_delta(reset_at),
                    "health": "good" if rem/lim > 0.3 else "warning",
                    "pace": "Stable",
                    "detail": f"{rem}/{lim} [API fallback]",
                })
        
        return cards
    except Exception as e:
        return [_error_card("GitHub Copilot", "🐙", f"Fail: {str(e)[:15]}")]

async def get_chatgpt():
    path = os.path.expanduser("~/.codex/sessions")
    try:
        files = glob.glob(f"{path}/**/*.jsonl", recursive=True)
        if not files: return [_error_card("ChatGPT Codex", "💬", "No logs")]
        latest = max(files, key=os.path.getmtime)
        with open(latest, "r") as f:
            lines = f.readlines()
            if not lines: return [_error_card("ChatGPT Codex", "💬", "Empty log")]
            usage = json.loads(lines[-1])
        pct = usage.get("used_percent", 0.0)
        reset_at = datetime.fromtimestamp(usage["resets_at"], tz=timezone.utc) if "resets_at" in usage else None
        return [{
            "service": "ChatGPT Codex",
            "icon": "💬",
            "remaining": f"{(100-pct):.1f}%",
            "unit": "remaining",
            "reset": _human_delta(reset_at),
            "health": "good" if pct < 80 else "warning",
            "pace": PaceCalculator.estimate_longevity(pct, reset_at),
            "detail": f"{pct:.1f}% used [Cache]",
        }]
    except: return [_error_card("ChatGPT Codex", "💬", "Parse Error")]

async def get_antigravity_ide():
    path = os.path.expanduser("~/.antigravity/state/quota.json")
    try:
        with open(path, "r") as f: data = json.load(f)
        res = []
        for name, usage in data.get("models", {}).items():
            rem = usage.get("remaining_percent", 0.0)
            reset = datetime.fromtimestamp(usage["resets_at"], tz=timezone.utc) if "resets_at" in usage else None
            res.append({
                "service": f"AG: {name}",
                "icon": "🛸",
                "remaining": f"{rem:.1f}%",
                "unit": "remaining",
                "reset": _human_delta(reset),
                "health": "good" if rem > 30 else "warning",
                "pace": PaceCalculator.estimate_longevity(100 - rem, reset),
                "detail": f"{name} [IDE]",
            })
        return res
    except: return None

async def get_opencode_go(client: httpx.AsyncClient):
    key = os.getenv("OPENCODE_GO_API_KEY")
    if not key: return []
    try:
        # Diagnostic showed GET /v1/user/usage is the way to go
        resp = await client.get("https://api.opencode.ai/v1/user/usage", headers={"Authorization": f"Bearer {key}"})
        if resp.status_code != 200: 
            return [_error_card("OpenCode Go", "🚀", f"HTTP {resp.status_code}")]
        
        try:
            data = resp.json()
        except:
            return [_error_card("OpenCode Go", "🚀", "Invalid JSON")]

        used, lim = data.get("total_usage_usd", 0), data.get("hard_limit_usd", 0)
        if lim == 0: return [_error_card("OpenCode Go", "🚀", "No limit set")]
        rem = max(0, lim - used)
        pct = (used / lim * 100)
        return [{
            "service": "OpenCode Go",
            "icon": "🚀",
            "remaining": f"${rem:.2f}",
            "unit": "USD",
            "reset": "Rolling 5h",
            "health": "good" if pct < 70 else "warning",
            "pace": "Stable",
            "detail": f"${used:.2f}/${lim:.2f} ({pct:.1f}%) [API]",
        }]
    except Exception as e: 
        return [_error_card("OpenCode Go", "🚀", f"Fail: {str(e)[:15]}")]

async def get_opencode_tui():
    db = os.path.expanduser("~/.local/share/opencode/opencode.db")
    if not os.path.exists(db): return None
    try:
        import aiosqlite
        async with aiosqlite.connect(db) as conn:
            async with conn.execute("SELECT SUM(summary_additions + summary_deletions) FROM session") as cursor:
                row = await cursor.fetchone()
                tokens = row[0] or 0
        return [{
            "service": "OpenCode TUI",
            "icon": "⚡",
            "remaining": f"{tokens:,}",
            "unit": "lines changed",
            "reset": "History",
            "health": "good",
            "pace": "Stable",
            "detail": "Local DB",
        }]
    except Exception as e: 
        return [_error_card("OpenCode TUI", "⚡", f"DB Error: {str(e)[:15]}")]

async def get_zai(client: httpx.AsyncClient):
    key = os.getenv("ZAI_API_KEY")
    if not key or "zai" in key: return [_error_card("zAI", "🌐", "Missing/Invalid Key")]
    try:
        resp = await client.get("https://open.bigmodel.cn/api/paas/v4/users/me/balance", headers={"Authorization": f"Bearer {key}"})
        if resp.status_code != 200: return [_error_card("zAI", "🌐", "API Error")]
        bal = float(resp.json().get("data", {}).get("available_balance", 0))
        return [{
            "service": "zAI (GLM)",
            "icon": "🌐",
            "remaining": f"¥{bal:.2f}",
            "unit": "balance",
            "reset": "Manual",
            "health": "good" if bal > 10 else "warning",
            "pace": "Stable",
            "detail": "Prepaid balance",
        }]
    except: return [_error_card("zAI", "🌐", "Connection Failed")]

async def get_kimi(client: httpx.AsyncClient):
    key = os.getenv("KIMI_API_KEY")
    if not key or len(key) < 10: return [_error_card("Kimi K2.5", "🌙", "Missing/Invalid Key")]
    try:
        resp = await client.get("https://api.moonshot.cn/v1/users/me/balance", headers={"Authorization": f"Bearer {key}"})
        if resp.status_code == 401: return [_error_card("Kimi K2.5", "🌙", "Unauthorized")]
        if resp.status_code != 200: return [_error_card("Kimi K2.5", "🌙", f"HTTP {resp.status_code}")]
        bal = float(resp.json().get("data", {}).get("available_balance", 0))
        return [{
            "service": "Kimi K2.5",
            "icon": "🌙",
            "remaining": f"${bal:.2f}",
            "unit": "balance",
            "reset": "Manual",
            "health": "good" if bal > 5 else "warning",
            "pace": "Stable",
            "detail": "Prepaid balance",
        }]
    except: return [_error_card("Kimi K2.5", "🌙", "Connection Failed")]

# ─────────────────────────────────────────────────────────────────────────────
# API
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/limits")
async def fetch_all_limits():
    oauth_token = os.getenv("CLAUDE_CODE_OAUTH_TOKEN")
    async with httpx.AsyncClient() as client:
        tasks = [
            get_claude_oauth(client, oauth_token) if oauth_token else get_claude_local(),
            get_gemini_cli(),
            get_github(client),
            get_chatgpt(),
            get_antigravity_ide(),
            get_opencode_go(client),
            get_opencode_tui(),
            get_zai(client),
            get_kimi(client)
        ]
        results = await asyncio.gather(*tasks)
    
    flattened = []
    for res in results:
        if isinstance(res, list): flattened.extend(res)
    return {"limits": flattened}

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    with open("index.html", "r") as f: return f.read()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765)