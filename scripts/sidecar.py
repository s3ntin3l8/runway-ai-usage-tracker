#!/usr/bin/env python3
"""
Runway Sidecar - Token and Local Data Collector

Architecture:
- Extracts tokens/cookies from local files and keychain
- Reads local data files (SQLite DBs, JSON logs)
- Sends tokens and data to Runway server via /api/ingest
- Server uses tokens to make API calls

IMPORTANT: This sidecar does NOT make API calls directly.
All API calls are done by the server using tokens we provide.
"""

import os
import sys
import json
import argparse
import datetime
import subprocess
import sqlite3
import socket
import hmac
import hashlib
import time
from pathlib import Path
from urllib import request, error


# --- HTTP Utilities ---

def http_post_signed(url, data, api_key):
    """POST data to URL with HMAC-SHA256 signature."""
    timestamp = str(int(time.time()))
    # Use compact separators to match Pydantic's default serialization
    body = json.dumps(data, separators=(',', ':')).encode("utf-8")
    
    signature = hmac.new(
        api_key.encode(),
        timestamp.encode() + body,
        hashlib.sha256
    ).hexdigest()
    
    headers = {
        "Content-Type": "application/json",
        "X-Signature": signature,
        "X-Timestamp": timestamp
    }
    
    req = request.Request(url, data=body, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8")), resp.getcode()
    except error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8")), e.code
        except:
            return e.reason, e.code
    except Exception as e:
        return str(e), 500


def human_delta(target_dt):
    """Format datetime as human-readable delta."""
    if not target_dt:
        return "—"
    now = datetime.datetime.now(datetime.timezone.utc)
    if target_dt.tzinfo is None:
        target_dt = target_dt.replace(tzinfo=datetime.timezone.utc)
    diff = target_dt - now
    seconds = int(diff.total_seconds())
    if seconds < 0:
        return "Just now"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


# --- Token Extractors ---

class AnthropicCollector:
    """Extract Claude OAuth tokens from local sources."""
    
    @staticmethod
    def get_keychain_credentials():
        """Extract credentials from macOS Keychain."""
        if sys.platform != "darwin":
            return None, None
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                keychain_data = result.stdout.strip()
                try:
                    data = json.loads(keychain_data)
                    oauth_data = data.get("claudeAiOauth", {})
                    return oauth_data.get("accessToken"), oauth_data.get("refreshToken")
                except json.JSONDecodeError:
                    if keychain_data.startswith("sk-"):
                        return keychain_data, None
        except:
            pass
        return None, None

    @staticmethod
    def collect():
        """Extract OAuth tokens, send to server for API call."""
        access_token = None
        refresh_token = None
        
        # Priority 1: Env var (access token only)
        access_token = os.getenv("CLAUDE_CODE_OAUTH_TOKEN")
        
        # Priority 2: Credentials file
        if not access_token:
            cred_path = Path.home() / ".claude" / ".credentials.json"
            if cred_path.exists():
                try:
                    with open(cred_path) as f:
                        data = json.load(f)
                        oauth_data = data.get("claudeAiOauth", {})
                        access_token = oauth_data.get("accessToken")
                        refresh_token = oauth_data.get("refreshToken")
                except:
                    pass
        
        # Priority 3: macOS Keychain
        if not access_token:
            access_token, refresh_token = AnthropicCollector.get_keychain_credentials()
        
        if not access_token:
            return []
        
        # Build token detail string
        detail_parts = [f"oauth_token:{access_token}"]
        if refresh_token:
            detail_parts.append(f"refresh_token:{refresh_token}")
        detail_parts.append("[Sidecar]")
        
        # Send tokens to server - server will make API call and handle refresh
        return [{
            "service": "Claude Pro",
            "icon": "🟠",
            "remaining": "Token",
            "unit": "oauth",
            "reset": "—",
            "health": "good",
            "pace": "Token",
            "detail": " ".join(detail_parts),
            "data_source": "token_extracted",
        }]


class GitHubCollector:
    """Extract GitHub token from local sources."""
    
    @staticmethod
    def collect():
        """Extract GitHub token, send to server for API call."""
        # Priority 1: Env var
        token = os.getenv("GITHUB_TOKEN")
        
        # Priority 2: gh CLI config
        if not token:
            gh_path = Path.home() / ".config" / "gh" / "hosts.yml"
            if gh_path.exists():
                try:
                    import yaml
                    with open(gh_path) as f:
                        data = yaml.safe_load(f)
                        if data and "github.com" in data:
                            token = data["github.com"].get("oauth_token")
                except:
                    pass
        
        if not token:
            return []
        
        return [{
            "service": "GitHub API",
            "icon": "🐙",
            "remaining": "Token",
            "unit": "api_key",
            "reset": "—",
            "health": "good",
            "pace": "Token",
            "detail": f"api_key:{token} [Sidecar]",
            "data_source": "token_extracted",
        }]


class GeminiCollector:
    """Extract Gemini OAuth credentials from local files."""
    
    @staticmethod
    def collect():
        """Extract OAuth credentials, send to server for API call."""
        creds_path = Path.home() / ".gemini" / "oauth_creds.json"
        
        if not creds_path.exists():
            return []
        
        try:
            with open(creds_path) as f:
                creds = json.load(f)
            
            token = creds.get("access_token")
            if not token:
                return []
            
            return [{
                "service": "Gemini API",
                "icon": "🔵",
                "remaining": "Token",
                "unit": "oauth",
                "reset": "—",
                "health": "good",
                "pace": "Token",
                "detail": f"oauth_token:{token} [Sidecar]",
                "data_source": "token_extracted",
            }]
        except:
            return []


class ChatGPTCollector:
    """Extract ChatGPT OAuth token from local sources."""
    
    @staticmethod
    def collect():
        """Extract OAuth token, send to server for API call."""
        # Priority 1: Env var
        token = os.getenv("CHATGPT_OAUTH_TOKEN")
        
        # Priority 2: Codex auth file
        if not token:
            auth_path = Path.home() / ".codex" / "auth.json"
            if auth_path.exists():
                try:
                    with open(auth_path) as f:
                        data = json.load(f)
                        token = data.get("tokens", {}).get("access_token")
                except:
                    pass
        
        if not token:
            return []
        
        return [{
            "service": "ChatGPT Codex",
            "icon": "💬",
            "remaining": "Token",
            "unit": "oauth",
            "reset": "—",
            "health": "good",
            "pace": "Token",
            "detail": f"oauth_token:{token} [Sidecar]",
            "data_source": "token_extracted",
        }]


class KimiCollector:
    """Extract Kimi auth token from Chrome cookies."""
    
    @staticmethod
    def _get_cookie():
        """Extract kimi-auth from Chrome cookies."""
        try:
            # Determine Chrome cookies path
            if sys.platform == "darwin":
                path = Path.home() / "Library/Application Support/Google/Chrome/Default/Cookies"
            elif sys.platform == "win32":
                path = Path.home() / "AppData/Local/Google/Chrome/User Data/Default/Network/Cookies"
                if not path.exists():
                    path = Path.home() / "AppData/Local/Google/Chrome/User Data/Default/Cookies"
            else:
                path = Path.home() / ".config/google-chrome/Default/Cookies"
                if not path.exists():
                    path = Path.home() / ".config/chromium/Default/Cookies"
            
            if not path.exists():
                return None
            
            conn = sqlite3.connect(str(path))
            cursor = conn.cursor()
            
            for cookie_name in ["kimi-auth", "kimi_token"]:
                cursor.execute(
                    "SELECT value FROM cookies WHERE host_key LIKE '%kimi.com%' AND name = ?",
                    (cookie_name,)
                )
                row = cursor.fetchone()
                if row:
                    conn.close()
                    return row[0]
            
            conn.close()
        except:
            pass
        return None
    
    @staticmethod
    def collect():
        """Extract Kimi cookie, send to server for API call."""
        # Priority 1: Env var
        token = os.getenv("KIMI_AUTH_TOKEN")
        
        # Priority 2: Chrome cookie
        if not token:
            token = KimiCollector._get_cookie()
        
        if not token:
            return []
        
        return [{
            "service": "Kimi API",
            "icon": "🌙",
            "remaining": "Token",
            "unit": "cookie",
            "reset": "—",
            "health": "good",
            "pace": "Token",
            "detail": f"cookie:kimi-auth:{token} [Sidecar]",
            "data_source": "token_extracted",
        }]


class ZaiCollector:
    """Extract ZAI API key from local sources."""
    
    @staticmethod
    def collect():
        """Extract API key, send to server for API call."""
        key = os.getenv("ZAI_API_KEY")
        
        if not key or key.lower() == "zai":
            return []
        
        return [{
            "service": "zAI API",
            "icon": "🌐",
            "remaining": "Token",
            "unit": "api_key",
            "reset": "—",
            "health": "good",
            "pace": "Token",
            "detail": f"api_key:{key} [Sidecar]",
            "data_source": "token_extracted",
        }]


# --- Local Data Collectors (Keep these as-is, they read local files) ---

class OpenCodeCollector:
    """Read OpenCode local database."""
    
    @staticmethod
    def get_chrome_cookies_path():
        """Get Chrome cookies database path."""
        system = sys.platform
        home = Path.home()
        
        if system == "darwin":
            path = home / "Library/Application Support/Google/Chrome/Default/Cookies"
        elif system == "win32":
            path = home / "AppData/Local/Google/Chrome/User Data/Default/Network/Cookies"
            if not path.exists():
                path = home / "AppData/Local/Google/Chrome/User Data/Default/Cookies"
        else:
            path = home / ".config/google-chrome/Default/Cookies"
            if not path.exists():
                path = home / ".config/chromium/Default/Cookies"
        
        return path if path.exists() else None
    
    @staticmethod
    def get_opencode_session():
        """Extract opencode.ai session from Chrome."""
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
        """Read local OpenCode DB and extract session cookie."""
        results = []
        hostname = socket.gethostname()
        
        # 1. Session cookie for server Web API
        session = OpenCodeCollector.get_opencode_session()
        if session:
            results.append({
                "service": "OpenCode (Web Token)",
                "icon": "⚡",
                "remaining": "Cookie",
                "unit": "web",
                "reset": "—",
                "health": "good",
                "pace": "Token",
                "detail": f"cookie:session:{session} [Sidecar]",
                "data_source": "token_extracted",
            })
        
        # 2. Local DB data
        db_path = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
        if db_path.exists():
            try:
                conn = sqlite3.connect(str(db_path))
                cursor = conn.cursor()
                
                now = datetime.datetime.now(datetime.timezone.utc)
                
                cutoffs = {
                    "5h": int((now - datetime.timedelta(hours=5)).timestamp() * 1000),
                    "week": int((now - datetime.timedelta(days=7)).timestamp() * 1000),
                    "month": int((now - datetime.timedelta(days=30)).timestamp() * 1000),
                }
                
                limits = {"5h": 12.0, "week": 30.0, "month": 60.0}
                
                for window, cutoff_ms in cutoffs.items():
                    cursor.execute("""
                        SELECT SUM(json_extract(data, '$.cost')), COUNT(*)
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
                    
                    window_labels = {"5h": "5 Hours", "week": "7 Days", "month": "30 Days"}
                    
                    results.append({
                        "service": f"OpenCode ({window_labels[window]})",
                        "icon": "⚡",
                        "remaining": f"${remaining:.2f}",
                        "unit": f"${limit:.0f} limit",
                        "reset": f"Rolling {window}",
                        "health": "good" if pct < 70 else "warning" if pct < 90 else "critical",
                        "pace": "Stable" if pct < 50 else "High" if pct < 80 else "Fatigue",
                        "detail": f"${used:.2f} used · {count} msgs · {hostname} [Sidecar]",
                        "data_source": "local",
                    })
                
                conn.close()
            except:
                pass
        
        return results


class AntigravityCollector:
    """Read Antigravity local quota file."""
    
    @staticmethod
    def collect():
        """Read local Antigravity quota file."""
        path = Path.home() / ".antigravity" / "state" / "quota.json"
        
        try:
            with open(path) as f:
                data = json.load(f)
            
            results = []
            for name, usage in data.get("models", {}).items():
                rem = usage.get("remaining_percent", 0.0)
                reset_ts = usage.get("resets_at")
                reset_at = datetime.datetime.fromtimestamp(reset_ts, tz=datetime.timezone.utc) if reset_ts else None
                
                results.append({
                    "service": f"AG: {name}",
                    "icon": "🛸",
                    "remaining": f"{rem:.1f}%",
                    "unit": "remaining",
                    "reset": human_delta(reset_at),
                    "health": "good" if rem > 30 else "warning",
                    "pace": "Stable",
                    "detail": f"{name} [Sidecar]",
                    "data_source": "local",
                })
            
            return results
        except:
            return []


# --- Main Script ---

def run_install(api_url, api_key):
    """Install sidecar as scheduled task."""
    print("\n--- Sidecar Installer ---")
    if not api_url:
        api_url = input("Enter Runway API URL (e.g. http://localhost:8765): ").strip()
    if not api_key:
        api_key = input("Enter Ingestion API Key: ").strip()
    
    script_path = os.path.abspath(__file__)
    
    if sys.platform == "win32":
        cmd = f'schtasks /create /tn "RunwaySidecar" /tr "python {script_path} --api-url {api_url} --api-key {api_key}" /sc minute /mo 30 /f'
        try:
            subprocess.run(cmd, shell=True, check=True)
            print("SUCCESS: Task Scheduler entry created (Every 30m).")
        except Exception as e:
            print(f"ERROR: Failed to create Task Scheduler entry: {e}")
    else:
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
    parser = argparse.ArgumentParser(description="Runway Sidecar - Token & Data Collector")
    parser.add_argument("--provider", default="all", help="Provider to collect")
    parser.add_argument("--api-url", help="Runway API URL")
    parser.add_argument("--api-key", help="Ingestion API Key")
    parser.add_argument("--install", action="store_true", help="Install as scheduled task")
    parser.add_argument("--dry-run", action="store_true", help="Print without pushing")
    
    args = parser.parse_args()
    
    if args.install:
        run_install(args.api_url, args.api_key)
        return
    
    # Collect from all providers
    all_metrics = []
    providers = []
    
    if args.provider == "all":
        providers = [
            AnthropicCollector,
            GitHubCollector,
            GeminiCollector,
            ChatGPTCollector,
            KimiCollector,
            ZaiCollector,
            OpenCodeCollector,
            AntigravityCollector,
        ]
    elif args.provider == "anthropic":
        providers = [AnthropicCollector]
    elif args.provider == "github":
        providers = [GitHubCollector]
    elif args.provider == "gemini":
        providers = [GeminiCollector]
    elif args.provider == "chatgpt":
        providers = [ChatGPTCollector]
    elif args.provider == "kimi":
        providers = [KimiCollector]
    elif args.provider == "zai":
        providers = [ZaiCollector]
    elif args.provider == "opencode":
        providers = [OpenCodeCollector]
    elif args.provider == "antigravity":
        providers = [AntigravityCollector]
    
    for p in providers:
        try:
            all_metrics.extend(p.collect())
        except Exception as e:
            print(f"ERROR collecting from {p.__name__}: {e}", file=sys.stderr)
    
    if not all_metrics:
        print("No metrics collected.")
        return
    
    if args.dry_run:
        print(f"Dry Run: {len(all_metrics)} metrics collected.")
        print(json.dumps(all_metrics, indent=2))
        return
    
    if not args.api_url or not args.api_key:
        print("ERROR: --api-url and --api-key required. Use --dry-run to test.")
        return
    
    # Determine provider name
    hostname = socket.gethostname()
    if args.provider == "all":
        provider_name = f"sidecar-{hostname}"
    else:
        provider_name = f"{args.provider}-{hostname}"
    
    # Push to server
    payload = {
        "provider": provider_name,
        "metrics": all_metrics
    }
    
    target_url = f"{args.api_url.rstrip('/')}/api/ingest"
    data, code = http_post_signed(target_url, payload, args.api_key)
    
    if code == 200:
        print(f"SUCCESS: Pushed {len(all_metrics)} metrics to {target_url}")
        resp = data
        print(f"  Tokens: {resp.get('tokens_received', 0)}, Metrics: {resp.get('metrics_stored', 0)}")
    else:
        print(f"ERROR {code}: {json.dumps(data) if isinstance(data, dict) else data}")


if __name__ == "__main__":
    main()
