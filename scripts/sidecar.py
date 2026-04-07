#!/usr/bin/env python3
import os
import sys
import json
import argparse
import datetime
import glob
import subprocess
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
    @staticmethod
    def collect():
        sessions_dir = Path.home() / ".gemini" / "tmp" / "sessions"
        try:
            files = list(sessions_dir.glob("*.jsonl"))
            if not files: return []
            total = 0
            for fpath in files:
                with open(fpath, "r") as f:
                    for line in f:
                        try:
                            u = json.loads(line).get("usage", {})
                            total += (u.get("prompt_tokens", 0) + u.get("completion_tokens", 0))
                        except: pass
            return [{
                "service": "Gemini CLI",
                "icon": "🔵",
                "remaining": f"{total:,}",
                "unit": "tokens (24h)",
                "reset": "Rolling 24h",
                "health": "good",
                "pace": "Stable",
                "detail": "Sidecar Scan"
            }]
        except: return []

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
    if args.provider == "all": providers = [AnthropicCollector, GitHubCollector, GeminiCollector]
    elif args.provider == "anthropic": providers = [AnthropicCollector]
    elif args.provider == "github": providers = [GitHubCollector]
    elif args.provider == "gemini": providers = [GeminiCollector]
    
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

    # Pushing
    payload = {
        "provider": "sidecar",
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
