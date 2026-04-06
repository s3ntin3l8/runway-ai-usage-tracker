"""
Runway — AI Subscription Limits Dashboard
FastAPI backend: collects live data from 8 sources and returns a single /api/limits JSON.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
import asyncio
import json
import os
import glob
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Set
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Runway — AI Limits Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

def _human_delta(dt: datetime | None) -> str:
    """Convert a future datetime to a human-readable countdown string."""
    if dt is None:
        return "—"
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = dt - now
    if delta.total_seconds() <= 0:
        return "Now"
    total = int(delta.total_seconds())
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


# ─────────────────────────────────────────────────────────────────────────────
# 1. CLAUDE PRO  — ~/.claude/projects/**/*.jsonl
# ─────────────────────────────────────────────────────────────────────────────
async def get_claude():
    projects_dir = os.path.expanduser(
        os.getenv("CLAUDE_PROJECTS_DIR", "~/.claude/projects")
    )
    limit = int(os.getenv("CLAUDE_5H_TOKEN_LIMIT", "2000000"))

    try:
        files = glob.glob(f"{projects_dir}/**/*.jsonl", recursive=True)
        if not files:
            raise FileNotFoundError("No session files found")

        cutoff = datetime.now(timezone.utc) - timedelta(hours=5)
        total_tokens = 0
        oldest_in_window: datetime | None = None

        for fpath in files:
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    for raw in f:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            entry = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        if entry.get("type") != "assistant":
                            continue

                        # Timestamp is stored as ISO in "timestamp" key at top level
                        ts_raw = entry.get("timestamp", "")
                        if not ts_raw:
                            continue
                        try:
                            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                        except ValueError:
                            continue

                        if ts < cutoff:
                            continue

                        usage = entry.get("message", {}).get("usage", {})
                        tokens = (
                            usage.get("input_tokens", 0)
                            + usage.get("output_tokens", 0)
                            + usage.get("cache_read_input_tokens", 0)
                            + usage.get("cache_creation_input_tokens", 0)
                        )
                        total_tokens += tokens

                        if oldest_in_window is None or ts < oldest_in_window:
                            oldest_in_window = ts
            except (PermissionError, OSError):
                continue

        remaining = max(0, limit - total_tokens)
        pct_used = (total_tokens / limit) * 100 if limit > 0 else 0
        health = "good" if pct_used < 70 else "warning" if pct_used < 90 else "critical"

        reset_at = (oldest_in_window + timedelta(hours=5)) if oldest_in_window else None
        reset_str = _human_delta(reset_at)

        return {
            "service": "Claude Pro",
            "icon": "🟠",
            "remaining": f"{remaining:,}",
            "unit": "tokens / 5h",
            "reset": reset_str if reset_at else "Rolling window",
            "health": health,
            "detail": f"{total_tokens:,} / {limit:,} used ({pct_used:.0f}%)",
        }
    except Exception as e:
        return {
            "service": "Claude Pro",
            "icon": "🟠",
            "remaining": "ERR",
            "unit": "—",
            "reset": "—",
            "health": "unknown",
            "detail": str(e),
        }


# ─────────────────────────────────────────────────────────────────────────────
# 2. GEMINI CLI  — ~/.gemini/tmp/*/chats/session-*.json
# ─────────────────────────────────────────────────────────────────────────────
async def get_gemini_cli():
    tmp_dir = os.path.expanduser(
        os.getenv("GEMINI_TMP_DIR", "~/.gemini/tmp")
    )
    try:
        files = glob.glob(f"{tmp_dir}/*/chats/session-*.json")
        if not files:
            raise FileNotFoundError("No Gemini CLI session files found")

        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        total_input = total_output = total_cached = session_count = 0
        models_seen: set[str] = set()

        for fpath in files:
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

            last_updated = data.get("lastUpdated", "")
            try:
                file_ts = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue

            if file_ts < cutoff:
                continue

            session_count += 1
            for msg in data.get("messages", []):
                if msg.get("type") != "gemini":
                    continue
                tok = msg.get("tokens", {})
                total_input += tok.get("input", 0)
                total_output += tok.get("output", 0)
                total_cached += tok.get("cached", 0)
                model = msg.get("model", "")
                if model:
                    models_seen.add(model)

        total = total_input + total_output
        model_str = ", ".join(sorted(models_seen)) if models_seen else "—"

        return {
            "service": "Gemini CLI",
            "icon": "🔵",
            "remaining": f"{total:,}",
            "unit": "tokens (24h)",
            "reset": "Rolling 24h",
            "health": "good",
            "detail": f"{session_count} sessions · {model_str}",
        }
    except Exception as e:
        return {
            "service": "Gemini CLI",
            "icon": "🔵",
            "remaining": "ERR",
            "unit": "—",
            "reset": "—",
            "health": "unknown",
            "detail": str(e),
        }


# ─────────────────────────────────────────────────────────────────────────────
# 3. OPENCODE TUI  — ~/.local/share/opencode/opencode.db (SQLite)
# ─────────────────────────────────────────────────────────────────────────────
async def get_opencode_tui():
    db_path = os.path.expanduser(
        os.getenv("OPENCODE_TUI_DB", "~/.local/share/opencode/opencode.db")
    )
    try:
        import aiosqlite

        cutoff_ms = int(
            (datetime.now(timezone.utc) - timedelta(hours=24)).timestamp() * 1000
        )

        async with aiosqlite.connect(db_path) as db:
            async with db.execute(
                """
                SELECT
                    SUM(json_extract(data, '$.tokens.input')),
                    SUM(json_extract(data, '$.tokens.output')),
                    SUM(json_extract(data, '$.cost')),
                    COUNT(*)
                FROM message
                WHERE time_created > ?
                  AND json_valid(data)
                  AND json_extract(data, '$.role') = 'assistant'
                """,
                (cutoff_ms,),
            ) as cursor:
                row = await cursor.fetchone()

        inp = int(row[0] or 0)
        out = int(row[1] or 0)
        cost = float(row[2] or 0.0)
        count = int(row[3] or 0)
        total = inp + out

        return {
            "service": "OpenCode TUI",
            "icon": "⚡",
            "remaining": f"{total:,}",
            "unit": "tokens (24h)",
            "reset": "Rolling 24h",
            "health": "good",
            "detail": f"{count} messages · ${cost:.4f} cost",
        }
    except Exception as e:
        return {
            "service": "OpenCode TUI",
            "icon": "⚡",
            "remaining": "ERR",
            "unit": "—",
            "reset": "—",
            "health": "unknown",
            "detail": str(e),
        }


# ─────────────────────────────────────────────────────────────────────────────
# 4. OPENCODE GO  — https://api.opencode.ai/v1 (cloud)
# ─────────────────────────────────────────────────────────────────────────────
async def get_opencode_go(client: httpx.AsyncClient):
    api_key = os.getenv("OPENCODE_GO_API_KEY", "")
    base = "https://api.opencode.ai/v1"

    if not api_key or api_key.startswith("sk-opencode-go-..."):
        return {
            "service": "OpenCode Go",
            "icon": "🚀",
            "remaining": "—",
            "unit": "No key set",
            "reset": "—",
            "health": "unknown",
            "detail": "Set OPENCODE_GO_API_KEY in .env",
        }

    headers = {"Authorization": f"Bearer {api_key}"}

    # Try multiple likely endpoints — log raw response so we can adapt
    for endpoint in ["/usage", "/quota", "/subscription", "/user", "/me"]:
        try:
            resp = await client.get(f"{base}{endpoint}", headers=headers, timeout=8)
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}

            if resp.status_code == 200:
                # Try to extract common quota patterns
                remaining = (
                    data.get("remaining_tokens")
                    or data.get("remaining")
                    or data.get("quota_remaining")
                )
                limit = (
                    data.get("monthly_limit_tokens")
                    or data.get("limit")
                    or data.get("quota")
                )
                if remaining is not None:
                    health = "good"
                    if limit:
                        pct = (remaining / limit) * 100
                        health = "good" if pct > 20 else "warning" if pct > 5 else "critical"
                    return {
                        "service": "OpenCode Go",
                        "icon": "🚀",
                        "remaining": f"{remaining:,}" if isinstance(remaining, int) else str(remaining),
                        "unit": "tokens",
                        "reset": "End of month",
                        "health": health,
                        "detail": f"via {endpoint}",
                    }

                # Unknown response shape — surface it for debugging
                return {
                    "service": "OpenCode Go",
                    "icon": "🚀",
                    "remaining": "?",
                    "unit": "See detail",
                    "reset": "—",
                    "health": "unknown",
                    "detail": f"{endpoint} → {json.dumps(data)[:120]}",
                }

            if resp.status_code == 401:
                break  # Key is wrong, don't keep trying
        except Exception:
            continue

    return {
        "service": "OpenCode Go",
        "icon": "🚀",
        "remaining": "ERR",
        "unit": "Auth failed",
        "reset": "—",
        "health": "critical",
        "detail": "Check OPENCODE_GO_API_KEY",
    }


# ─────────────────────────────────────────────────────────────────────────────
# 5. GITHUB COPILOT  — https://api.github.com/rate_limit
# ─────────────────────────────────────────────────────────────────────────────
async def get_github(client: httpx.AsyncClient):
    token = os.getenv("GITHUB_TOKEN", "")

    if not token or token.startswith("ghp_..."):
        return {
            "service": "GitHub Copilot",
            "icon": "🐙",
            "remaining": "—",
            "unit": "No key set",
            "reset": "—",
            "health": "unknown",
            "detail": "Set GITHUB_TOKEN in .env",
        }

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        resp = await client.get(
            "https://api.github.com/rate_limit", headers=headers, timeout=8
        )
        resp.raise_for_status()
        resources = resp.json().get("resources", {})
        core = resources.get("core", {})

        remaining = core.get("remaining", 0)
        limit = core.get("limit", 5000)
        reset_unix = core.get("reset", 0)
        reset_at = datetime.fromtimestamp(reset_unix, tz=timezone.utc) if reset_unix else None

        pct = (remaining / limit * 100) if limit > 0 else 0
        health = "good" if pct > 30 else "warning" if pct > 10 else "critical"

        return {
            "service": "GitHub Copilot",
            "icon": "🐙",
            "remaining": f"{remaining:,}",
            "unit": "API requests",
            "reset": _human_delta(reset_at),
            "health": health,
            "detail": f"{remaining:,} / {limit:,} ({pct:.0f}% left)",
        }
    except Exception as e:
        return {
            "service": "GitHub Copilot",
            "icon": "🐙",
            "remaining": "ERR",
            "unit": "—",
            "reset": "—",
            "health": "unknown",
            "detail": str(e),
        }


# ─────────────────────────────────────────────────────────────────────────────
# 6. zAI GLM  — https://api.z.ai/api/monitor/usage/quota/limit
# ─────────────────────────────────────────────────────────────────────────────
async def get_zai(client: httpx.AsyncClient):
    # Uses the official Z.ai monitor API endpoints discovered by the VS Code extension community
    api_key = os.getenv("ZAI_API_KEY")
    if not api_key or api_key.startswith("sk-zai-..."):
        return {
            "service": "zAI (GLM Coding)",
            "icon": "🌐",
            "remaining": "—",
            "unit": "No key set",
            "reset": "—",
            "health": "unknown",
            "detail": "Set ZAI_API_KEY in .env",
        }

    url = "https://api.z.ai/api/monitor/usage/quota/limit"
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        resp = await client.get(url, headers=headers, timeout=8)
        resp.raise_for_status()
        data = resp.json()

        # The monitor API returns raw token usage and percentage for the 5-hour window
        remaining = data.get("remainingTokens", 0)
        percentage_used = data.get("percentage", 0)

        # Health indicator: GLM Coding plans warn at 80% used
        health = "good" if percentage_used < 80 else "critical"

        # Format numbers cleanly (e.g., 14,600 -> 14.6K)
        if remaining >= 1000000:
            remaining_display = f"{(remaining / 1000000):.1f}M"
        elif remaining >= 1000:
            remaining_display = f"{(remaining / 1000):.1f}K"
        else:
            remaining_display = str(remaining)

        return {
            "service": "zAI (GLM Coding)",
            "icon": "🌐",
            "remaining": remaining_display,
            "unit": "Tokens (5h)",
            "reset": "Rolling",
            "health": health,
            "detail": f"{percentage_used:.1f}% used",
        }
    except Exception as e:
        return {
            "service": "zAI (GLM Coding)",
            "icon": "🌐",
            "remaining": "ERR",
            "unit": "API Error",
            "reset": "—",
            "health": "critical",
            "detail": str(e),
        }


# ─────────────────────────────────────────────────────────────────────────────
# 7. KIMI K2.5  — https://api.moonshot.cn/v1/users/me/balance
# ─────────────────────────────────────────────────────────────────────────────
async def get_kimi(client: httpx.AsyncClient):
    api_key = os.getenv("KIMI_API_KEY", "")
    warn_balance = float(os.getenv("KIMI_WARNING_BALANCE", "10.0"))
    crit_balance = float(os.getenv("KIMI_CRITICAL_BALANCE", "2.0"))

    if not api_key or api_key.startswith("sk-moonshot-..."):
        return {
            "service": "Kimi K2.5",
            "icon": "🌙",
            "remaining": "—",
            "unit": "No key set",
            "reset": "—",
            "health": "unknown",
            "detail": "Set KIMI_API_KEY in .env",
        }

    try:
        resp = await client.get(
            "https://api.moonshot.cn/v1/users/me/balance",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=8,
        )
        resp.raise_for_status()
        balance = float(resp.json().get("data", {}).get("available_balance", 0))
        health = "good" if balance > warn_balance else "warning" if balance > crit_balance else "critical"

        return {
            "service": "Kimi K2.5",
            "icon": "🌙",
            "remaining": f"${balance:,.2f}",
            "unit": "balance",
            "reset": "Manual top-up",
            "health": health,
            "detail": f"Prepaid balance remaining",
        }
    except Exception as e:
        return {
            "service": "Kimi K2.5",
            "icon": "🌙",
            "remaining": "ERR",
            "unit": "—",
            "reset": "—",
            "health": "unknown",
            "detail": str(e),
        }


# ─────────────────────────────────────────────────────────────────────────────
# 8. CHATGPT (via Antigravity extension)  — placeholder
# ─────────────────────────────────────────────────────────────────────────────
async def get_chatgpt():
    return {
        "service": "ChatGPT Plus",
        "icon": "💬",
        "remaining": "—",
        "unit": "Via Antigravity ext",
        "reset": "—",
        "health": "unknown",
        "detail": "No local data · check chatgpt.com/settings",
    }


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENDPOINT
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/limits")
async def fetch_all_limits():
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            get_claude(),
            get_gemini_cli(),
            get_opencode_tui(),
            get_opencode_go(client),
            get_github(client),
            get_zai(client),
            get_kimi(client),
            get_chatgpt(),
        )
    return {"limits": list(results)}


@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    html_path = Path(__file__).parent / "index.html"
    return html_path.read_text(encoding="utf-8")