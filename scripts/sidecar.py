#!/usr/bin/env python3
import os
import sys
import json
import argparse
import datetime
import glob
import subprocess
import sqlite3
import socket
from pathlib import Path
from urllib import request, error

# --- Lightweight Utilities (In-script) ---

def human_delta(target_dt):
    if not target_dt: return "—"
    now = datetime.datetime.now(datetime.timezone.utc)
    if target_dt.tzinfo is None: target_dt = target_dt.replace(tzinfo=datetime.timezone.utc)
    diff = target_dt - now
    seconds = int(diff.total_seconds())
    if seconds < 0: return "Just now"
    if seconds < 60: return f"{seconds}s"
    if seconds < 3600: return f"{seconds // 60}m"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"

def http_post(url, data, headers=None):
    headers = headers or {}
    req = request.Request(url, data=json.dumps(data).encode("utf-8"), headers=headers, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8")), resp.getcode()
    except error.HTTPError as e:
        return e.read().decode("utf-8"), e.code
    except Exception as e:
        return str(e), 500

def http_get(url, headers=None):
    headers = headers or {}
    req = request.Request(url, headers=headers, method="GET")
    try:
        with request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8")), resp.getcode()
    except error.HTTPError as e:
        return e.read().decode("utf-8"), e.code
    except Exception as e:
        return str(e), 500

# --- Providers ---

class AnthropicCollector:
    @staticmethod
    def collect():
        token = os.getenv("CLAUDE_CODE_OAUTH_TOKEN")
        if not token:
            cred_path = Path.home() / ".claude" / ".credentials.json"
            if cred_path.exists():
                try:
                    with open(cred_path, "r") as f:
                        data = json.load(f)
                        token = data.get("claudeAiOauth", {}).get("accessToken")
                except: pass
        
        if not token: return []
        
        url = "https://api.anthropic.com/api/oauth/usage"
        headers = {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"}
        data, code = http_get(url, headers)
        
        if code != 200: return []
        
        name_map = {
            "five_hour": "Session Window",
            "seven_day": "Weekly Window",
            "seven_day_sonnet": "Sonnet Weekly",
            "seven_day_opus": "Opus Weekly",
            "extra_usage": "Extra Usage"
        }
        
        results = []
        for key, usage in data.items():
            if not isinstance(usage, dict) or "utilization" not in usage: continue
            u_type = name_map.get(key, key.replace("_", " ").title())
            pct = usage.get("utilization")
            if pct is None: pct = 0.0
            
            reset_raw = usage.get("resets_at") or usage.get("resetsAt")
            reset_at = None
            if reset_raw:
                try: reset_at = datetime.datetime.fromisoformat(reset_raw.replace("Z", "+00:00"))
                except: pass
            
            results.append({
                "service": f"Claude ({u_type})",
                "icon": "🟠",
                "remaining": f"{100-pct:.1f}%",
                "unit": "capacity",
                "reset": human_delta(reset_at),
                "health": "good" if pct < 70 else "warning" if pct < 90 else "critical",
                "pace": "Stable" if pct < 50 else "High Burn",
                "detail": f"{pct:.1f}% used [Sidecar]"
            })
        return results

class GitHubCollector:
    @staticmethod
    def collect():
        token = os.getenv("GITHUB_TOKEN")
        if not token: return []
        
        headers = {"Authorization": f"token {token}", "Accept": "application/json"}
        # Fetching basic rate limit for now (as lightweight fallback)
        data, code = http_get("https://api.github.com/rate_limit", headers)
        if code != 200: return []
        
        core = data.get("resources", {}).get("core", {})
        rem, lim = core.get("remaining", 0), core.get("limit", 1)
        reset_at = datetime.datetime.fromtimestamp(core.get("reset", 0), datetime.timezone.utc)
        
        return [{
            "service": "GitHub API",
            "icon": "🐙",
            "remaining": f"{rem:,}",
            "unit": "requests",
            "reset": human_delta(reset_at),
            "health": "good" if rem/lim > 0.3 else "warning",
            "pace": "Stable",
            "detail": f"{rem}/{lim} [Sidecar]"
        }]

class GeminiCollector:
    # Google Gemini CLI OAuth Credentials (from environment)
    @classmethod
    def get_client_id(cls):
        import os
        return os.getenv("GEMINI_OAUTH_CLIENT_ID", "")
    
    @classmethod
    def get_client_secret(cls):
        import os
        return os.getenv("GEMINI_OAUTH_CLIENT_SECRET", "")

    # Model display name mapping
    MODEL_DISPLAY_NAMES = {
        "gemini-2.5-flash": "Gemini 2.5 Flash",
        "gemini-2.5-flash-lite": "Gemini 2.5 Flash Lite",
        "gemini-2.5-pro": "Gemini 2.5 Pro",
        "gemini-3-flash-preview": "Gemini 3 Flash (Preview)",
        "gemini-3-pro-preview": "Gemini 3 Pro (Preview)",
        "gemini-3.1-flash-lite-preview": "Gemini 3.1 Flash Lite (Preview)",
        "gemini-3.1-pro-preview": "Gemini 3.1 Pro (Preview)",
    }

    @staticmethod
    def collect():
        results = []
        creds_path = Path.home() / ".gemini" / "oauth_creds.json"
        
        # 1. API Collection
        if creds_path.exists():
            try:
                with open(creds_path, "r") as f:
                    creds = json.load(f)
                
                # Check expiry
                import time
                if creds.get("expiry_date", 0) < (time.time() * 1000):
                    creds = GeminiCollector._refresh_token(creds)
                    if creds:
                        with open(creds_path, "w") as f:
                            json.dump(creds, f, indent=2)
                
                if creds:
                    token = creds.get("access_token")
                    headers = {"Authorization": f"Bearer {token}"}
                    
                    # Fetch Tier and Project first (needed for gemini-3 models)
                    tier_url = "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"
                    t_data, t_code = http_post(tier_url, {"metadata": {"ideType": "GEMINI_CLI", "pluginType": "GEMINI"}}, headers)
                    
                    # Get project and tier info
                    project_id = t_data.get("cloudaicompanionProject", "") if t_code == 200 else ""
                    current_tier = t_data.get("currentTier", {}) if t_code == 200 else {}
                    tier_name = current_tier.get("name", "Unknown Tier")
                    
                    # Fetch Quota with discovered project (required for gemini-3 models)
                    quota_url = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota"
                    q_data, q_code = http_post(quota_url, {"project": project_id}, headers)
                    
                    if q_code == 200:
                        buckets = q_data.get("buckets", [])
                        if buckets:
                            # Create one card per model bucket
                            for bucket in buckets:
                                model_id = bucket.get("modelId", "Unknown")
                                display_name = GeminiCollector.MODEL_DISPLAY_NAMES.get(model_id, model_id)
                                
                                # remainingFraction: 1.0 = 100% remaining = 0% used
                                remaining_fraction = bucket.get("remainingFraction", 1.0)
                                percent_remaining = int(remaining_fraction * 100)
                                percent_used = 100 - percent_remaining
                                
                                # Format reset time
                                reset_str = "Resetting..."
                                if "resetTime" in bucket:
                                    try:
                                        reset_str = f"Resets {bucket['resetTime'].split('T')[-1][:5]}"
                                    except:
                                        reset_str = f"Resets {bucket['resetTime']}"
                                
                                # Determine health based on % used
                                if percent_used < 50:
                                    health = "good"
                                elif percent_used < 80:
                                    health = "warning"
                                else:
                                    health = "critical"
                                
                                results.append({
                                    "service": display_name,
                                    "icon": "🔵",
                                    "remaining": f"{percent_used}%",
                                    "unit": "used",
                                    "reset": reset_str,
                                    "health": health,
                                    "pace": tier_name,
                                    "detail": f"{percent_remaining}% remaining | Model: {model_id} [Sidecar]"
                                })
                            
                            # Sort by usage (highest % used first)
                            results.sort(key=lambda x: int(x["remaining"].rstrip("%")), reverse=True)
            except: pass

        # 2. Fallback to Logs
        if not results:
            sessions_dir = Path.home() / ".gemini" / "tmp" / "sessions"
            try:
                files = list(sessions_dir.glob("*.jsonl"))
                if files:
                    total = 0
                    for fpath in files:
                        with open(fpath, "r") as f:
                            for line in f:
                                try:
                                    u = json.loads(line).get("usage", {})
                                    total += (u.get("prompt_tokens", 0) + u.get("completion_tokens", 0))
                                except: pass
                    results.append({
                        "service": "Gemini CLI",
                        "icon": "🔵",
                        "remaining": f"{total:,}",
                        "unit": "tokens (24h)",
                        "reset": "Rolling 24h",
                        "health": "good",
                        "pace": "Stable",
                        "detail": "Logs [Sidecar]"
                    })
            except: pass
            
        return results

    @staticmethod
    def _refresh_token(creds):
        refresh_token = creds.get("refresh_token")
        if not refresh_token: return None
        
        payload = f"client_id={GeminiCollector.get_client_id()}&client_secret={GeminiCollector.get_client_secret()}&refresh_token={refresh_token}&grant_type=refresh_token"
        req = request.Request("https://oauth2.googleapis.com/token", data=payload.encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        
        try:
            with request.urlopen(req, timeout=10) as resp:
                new_data = json.loads(resp.read().decode("utf-8"))
                creds["access_token"] = new_data["access_token"]
                import time
                creds["expiry_date"] = int(time.time() * 1000) + (new_data["expires_in"] * 1000)
                return creds
        except:
            return None
class ChatGPTCollector:
    @staticmethod
    def collect():
        token = os.getenv("CHATGPT_OAUTH_TOKEN")
        if not token:
            auth_path = Path.home() / ".codex" / "auth.json"
            if auth_path.exists():
                try:
                    with open(auth_path, "r") as f:
                        data = json.load(f)
                        token = data.get("tokens", {}).get("access_token")
                except: pass
        
        results = []
        if token:
            url = "https://chatgpt.com/backend-api/wham/usage"
            headers = {"Authorization": f"Bearer {token}"}
            data, code = http_get(url, headers)
            if code == 200:
                primary = data.get("primary", {})
                pct = primary.get("utilization_percent", 0.0)
                reset_ts = primary.get("resets_at")
                reset_at = datetime.datetime.fromtimestamp(reset_ts, datetime.timezone.utc) if reset_ts else None
                
                results.append({
                    "service": "ChatGPT Codex",
                    "icon": "💬",
                    "remaining": f"{100-pct:.1f}%",
                    "unit": "remaining",
                    "reset": human_delta(reset_at),
                    "health": "good" if pct < 80 else "warning",
                    "pace": "Stable" if pct < 50 else "High Burn",
                    "detail": "API [Sidecar]"
                })
        
        # Log fallback (if API failed or no token)
        if not results:
            sessions_dir = Path.home() / ".codex" / "sessions"
            try:
                files = list(sessions_dir.glob("**/*.jsonl"))
                if files:
                    latest = max(files, key=os.path.getmtime)
                    with open(latest, "r") as f:
                        lines = f.readlines()
                        if lines:
                            usage = json.loads(lines[-1])
                            pct = usage.get("used_percent", 0.0)
                            reset_ts = usage.get("resets_at")
                            reset_at = datetime.datetime.fromtimestamp(reset_ts, datetime.timezone.utc) if reset_ts else None
                            results.append({
                                "service": "ChatGPT Codex",
                                "icon": "💬",
                                "remaining": f"{100-pct:.1f}%",
                                "unit": "remaining",
                                "reset": human_delta(reset_at),
                                "health": "good" if pct < 80 else "warning",
                                "pace": "Stable",
                                "detail": "Log [Sidecar]"
                            })
            except: pass
            
        return results

class OpenCodeCollector:
    @staticmethod
    def get_chrome_cookies_path():
        """Get Chrome cookies database path based on platform."""
        system = sys.platform
        home = Path.home()
        
        if system == "darwin":  # macOS
            path = home / "Library/Application Support/Google/Chrome/Default/Cookies"
        elif system == "win32":  # Windows
            path = home / "AppData/Local/Google/Chrome/User Data/Default/Network/Cookies"
            if not path.exists():
                path = home / "AppData/Local/Google/Chrome/User Data/Default/Cookies"
        else:  # Linux
            path = home / ".config/google-chrome/Default/Cookies"
            if not path.exists():
                path = home / ".config/chromium/Default/Cookies"
        
        return path if path.exists() else None
    
    @staticmethod
    def get_opencode_session():
        """Extract opencode.ai session cookie from Chrome (if available)."""
        cookies_path = OpenCodeCollector.get_chrome_cookies_path()
        if not cookies_path:
            return None
        
        try:
            conn = sqlite3.connect(str(cookies_path))
            cursor = conn.cursor()
            cursor.execute(
                "SELECT value FROM cookies WHERE host_key LIKE '%opencode.ai%' AND name = 'session'"
            )
            row = cursor.fetchone()
            conn.close()
            return row[0] if row else None
        except:
            return None
    
    @staticmethod
    def collect():
        """
        Collect OpenCode usage from local database.
        
        Note: This sidecar sends local DB data. The main app will:
        1. Try web API with Chrome cookies first (aggregates all devices)
        2. Fall back to aggregating sidecar data from multiple hosts
        """
        db_path = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
        if not db_path.exists(): 
            # Try to extract and send cookie instead
            session = OpenCodeCollector.get_opencode_session()
            if session:
                # Return a special marker that tells main app to use web API
                hostname = socket.gethostname()
                return [{
                    "service": "OpenCode (Cookie)",
                    "icon": "⚡",
                    "remaining": "Web API",
                    "unit": "session",
                    "reset": "—",
                    "health": "good",
                    "pace": "Stable",
                    "detail": f"session:{session} · {hostname} [Sidecar]",
                }]
            return []
        
        try:
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            
            now = datetime.datetime.now(datetime.timezone.utc)
            hostname = socket.gethostname()
            
            # Calculate cutoff times for each window
            cutoffs = {
                "5h": int((now - datetime.timedelta(hours=5)).timestamp() * 1000),
                "week": int((now - datetime.timedelta(days=7)).timestamp() * 1000),
                "month": int((now - datetime.timedelta(days=30)).timestamp() * 1000),
            }
            
            # Documented limits for OpenCode Go
            limits = {
                "5h": 12.0,
                "week": 30.0,
                "month": 60.0,
            }
            
            results = []
            
            for window, cutoff_ms in cutoffs.items():
                cursor.execute("""
                    SELECT 
                        SUM(json_extract(data, '$.cost')),
                        COUNT(*)
                    FROM message
                    WHERE time_created > ?
                      AND json_valid(data)
                      AND json_extract(data, '$.role') = 'assistant'
                """, (cutoff_ms,))
                row = cursor.fetchone()
                
                used = float(row[0] or 0.0)
                count = int(row[1] or 0)
                limit = limits[window]
                remaining = max(0, limit - used)
                pct = (used / limit * 100) if limit > 0 else 0
                
                # Format window label
                window_labels = {
                    "5h": "5 Hours",
                    "week": "7 Days",
                    "month": "30 Days"
                }
                
                results.append({
                    "service": f"OpenCode ({window_labels[window]})",
                    "icon": "⚡",
                    "remaining": f"${remaining:.2f}",
                    "unit": f"${limit:.0f} limit",
                    "reset": f"Rolling {window}",
                    "health": "good" if pct < 70 else "warning" if pct < 90 else "critical",
                    "pace": "Stable" if pct < 50 else "High" if pct < 80 else "Fatigue",
                    "detail": f"${used:.2f} used · {count} msgs · {hostname} [Sidecar]",
                })
            
            conn.close()
            return results
        except:
            return []

# --- Main Script Logic ---

def run_install(api_url, api_key):
    print("\n--- Sidecar Installer ---")
    if not api_url: api_url = input("Enter Runway API URL (e.g. http://localhost:8765): ").strip()
    if not api_key: api_key = input("Enter Ingestion API Key: ").strip()
    
    script_path = os.path.abspath(__file__)
    
    if sys.platform == "win32":
        cmd = f'schtasks /create /tn "RunwaySidecar" /tr "python {script_path} --api-url {api_url} --api-key {api_key}" /sc minute /mo 30 /f'
        try:
            subprocess.run(cmd, shell=True, check=True)
            print("SUCCESS: Task Scheduler entry created (Every 30m).")
        except Exception as e:
            print(f"ERROR: Failed to create Task Scheduler entry: {e}")
    else:
        # Mac/Linux crontab
        cron_entry = f"*/30 * * * * {sys.executable} {script_path} --api-url {api_url} --api-key {api_key} > /dev/null 2>&1\n"
        try:
            current_cron = subprocess.check_output("crontab -l", shell=True, stderr=subprocess.STDOUT).decode("utf-8")
        except:
            current_cron = ""
        
        if script_path in current_cron:
            print("INFO: Task already exists in crontab. Updating...")
            lines = [l for l in current_cron.splitlines() if script_path not in l]
            current_cron = "\n".join(lines) + "\n"
        
        with subprocess.Popen(["crontab", "-"], stdin=subprocess.PIPE) as proc:
            proc.communicate(input=(current_cron + cron_entry).encode("utf-8"))
        
        print("SUCCESS: Crontab entry created (Every 30m).")

def main():
    parser = argparse.ArgumentParser(description="Universal Lightweight Sidecar for Runway")
    parser.add_argument("--provider", default="all", help="Provider to collect (anthropic, github, gemini, all)")
    parser.add_argument("--api-url", help="Runway API URL")
    parser.add_argument("--api-key", help="Ingestion API Key")
    parser.add_argument("--install", action="store_true", help="Install as a background task")
    parser.add_argument("--dry-run", action="store_true", help="Print metrics without pushing")
    
    args = parser.parse_args()
    
    if args.install:
        run_install(args.api_url, args.api_key)
        return

    # Collection
    all_metrics = []
    providers = []
    if args.provider == "all": providers = [AnthropicCollector, GitHubCollector, GeminiCollector, ChatGPTCollector, OpenCodeCollector]
    elif args.provider == "anthropic": providers = [AnthropicCollector]
    elif args.provider == "github": providers = [GitHubCollector]
    elif args.provider == "gemini": providers = [GeminiCollector]
    elif args.provider == "chatgpt": providers = [ChatGPTCollector]
    elif args.provider == "opencode": providers = [OpenCodeCollector]
    
    for p in providers:
        all_metrics.extend(p.collect())
    
    if not all_metrics:
        print("No metrics collected.")
        return

    if args.dry_run:
        print(f"Dry Run: {len(all_metrics)} metrics collected.")
        print(json.dumps(all_metrics, indent=2))
        return

    if not args.api_url or not args.api_key:
        print("ERROR: --api-url and --api-key are required to push metrics. Use --dry-run to test.")
        return

    # Determine provider name based on collection type
    hostname = socket.gethostname()
    if args.provider == "opencode":
        provider_name = f"opencode-{hostname}"
    elif args.provider == "all":
        provider_name = f"sidecar-{hostname}"
    else:
        provider_name = f"{args.provider}-{hostname}"

    # Pushing
    payload = {
        "provider": provider_name,
        "api_key": args.api_key,
        "metrics": all_metrics
    }
    
    target_url = f"{args.api_url.rstrip('/')}/api/ingest"
    data, code = http_post(target_url, payload)
    
    if code == 200:
        print(f"SUCCESS: Pushed {len(all_metrics)} metrics to {target_url}")
    else:
        print(f"ERROR {code}: {data}")

if __name__ == "__main__":
    main()
