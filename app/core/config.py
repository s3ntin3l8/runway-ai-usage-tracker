import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    PROJECT_NAME: str = "Runway — AI Limits Dashboard"
    GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")
    
    @property
    def CLAUDE_CODE_OAUTH_TOKEN(self) -> str:
        # Priority 1: Env var
        token = os.getenv("CLAUDE_CODE_OAUTH_TOKEN", "")
        if token: return token
        
        # Priority 2: ~/.claude/.credentials.json (Claude Code)
        cred_path = os.path.expanduser("~/.claude/.credentials.json")
        if os.path.exists(cred_path):
            try:
                import json
                with open(cred_path, "r") as f:
                    data = json.load(f)
                    val = data.get("claudeAiOauth", {}).get("accessToken")
                    if val: return val
            except:
                pass
        return ""

    OPENCODE_GO_API_KEY: str = os.getenv("OPENCODE_GO_API_KEY", "")
    ZAI_API_KEY: str = os.getenv("ZAI_API_KEY", "")
    KIMI_API_KEY: str = os.getenv("KIMI_API_KEY", "")
    INGEST_API_KEY: str = os.getenv("INGEST_API_KEY", "sidecar-default-secret")
    
    # Path settings
    CLAUDE_PROJECTS_DIR: str = os.path.expanduser("~/.claude/projects")
    GEMINI_SESSIONS_DIR: str = os.path.expanduser("~/.gemini/tmp/sessions")
    CHATGPT_SESSIONS_DIR: str = os.path.expanduser("~/.codex/sessions")
    ANTIGRAVITY_QUOTA_PATH: str = os.path.expanduser("~/.antigravity/state/quota.json")
    OPENCODE_DB_PATH: str = os.path.expanduser("~/.local/share/opencode/opencode.db")
    EXTERNAL_METRICS_PATH: str = os.path.expanduser("~/.usage-tracker/external_metrics.json")

settings = Settings()
