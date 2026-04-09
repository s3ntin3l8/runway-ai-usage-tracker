import os
import json
import logging
import platform
import subprocess
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


def get_platform_data_dir(app_name: str) -> str:
    """Get the platform-specific directory for user data."""
    system = platform.system()
    home = os.path.expanduser("~")
    
    if system == "Windows":
        local_app_data = os.getenv("LOCALAPPDATA")
        if local_app_data:
            return os.path.join(local_app_data, app_name)
        return os.path.join(home, "AppData", "Local", app_name)
    elif system == "Darwin":  # macOS
        return os.path.join(home, "Library", "Application Support", app_name)
    else:  # Linux / Other
        xdg_data_home = os.getenv("XDG_DATA_HOME")
        if xdg_data_home:
            return os.path.join(xdg_data_home, app_name)
        return os.path.join(home, ".local", "share", app_name)


def get_platform_config_dir(app_name: str) -> str:
    """Get the platform-specific directory for user configuration."""
    system = platform.system()
    home = os.path.expanduser("~")
    
    if system == "Windows":
        app_data = os.getenv("APPDATA")
        if app_data:
            return os.path.join(app_data, app_name)
        return os.path.join(home, "AppData", "Roaming", app_name)
    elif system == "Darwin":  # macOS
        return os.path.join(home, "Library", "Application Support", app_name)
    else:  # Linux / Other
        xdg_config_home = os.getenv("XDG_CONFIG_HOME")
        if xdg_config_home:
            return os.path.join(xdg_config_home, app_name)
        return os.path.join(home, ".config", app_name)


DEFAULT_INGEST_API_KEY = "sidecar-default-secret"


class Settings:
    PROJECT_NAME: str = "Runway — AI Limits Dashboard"
    RUN_MODE: str = os.getenv("RUN_MODE", "standalone") # "standalone", "multi-host", "docker"
    GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")

    @property
    def CLAUDE_CODE_OAUTH_TOKEN(self) -> str:
        # Priority 1: Env var
        token = os.getenv("CLAUDE_CODE_OAUTH_TOKEN", "")
        if token:
            return token

        # Priority 2: Claude Code credentials (search multiple locations)
        home = os.path.expanduser("~")
        potential_paths = [
            os.path.join(home, ".claude", ".credentials.json"),
            os.path.join(get_platform_config_dir("claude"), ".credentials.json"),
        ]
        
        for cred_path in potential_paths:
            if os.path.exists(cred_path):
                try:
                    with open(cred_path, "r") as f:
                        data = json.load(f)
                        val = data.get("claudeAiOauth", {}).get("accessToken")
                        if val:
                            return val
                except Exception as e:
                    logger.debug(f"Error reading credentials from {cred_path}: {e}")

        # Priority 3: macOS Keychain (for sidecar scenarios)
        if platform.system() == "Darwin":
            try:
                result = subprocess.run(
                    ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0:
                    keychain_data = result.stdout.strip()
                    # Keychain stores the entire credentials JSON
                    try:
                        data = json.loads(keychain_data)
                        val = data.get("claudeAiOauth", {}).get("accessToken")
                        if val:
                            logger.debug("Found Claude OAuth token in macOS Keychain")
                            return val
                    except json.JSONDecodeError:
                        # Might be stored as raw token string
                        if keychain_data.startswith("sk-"):
                            return keychain_data
            except subprocess.TimeoutExpired:
                logger.debug("Keychain access timed out")
            except Exception as e:
                logger.debug(f"Could not read from macOS Keychain: {e}")

        # Priority 4: Python keyring library (cross-platform)
        try:
            import keyring
            token = keyring.get_password("runway", "claude-oauth-token")
            if token:
                logger.debug("Found Claude OAuth token in system keyring")
                return token
        except ImportError:
            logger.debug("keyring library not installed, skipping keyring retrieval")
        except Exception as e:
            logger.debug(f"Could not read from keyring: {e}")

        return ""

    OPENCODE_GO_API_KEY: str = os.getenv("OPENCODE_GO_API_KEY", "")
    ZAI_API_KEY: str = os.getenv("ZAI_API_KEY", "")
    KIMI_API_KEY: str = os.getenv("KIMI_API_KEY", "")
    KIMI_AUTH_TOKEN: str = os.getenv("KIMI_AUTH_TOKEN", "")
    INGEST_API_KEY: str = os.getenv("INGEST_API_KEY", DEFAULT_INGEST_API_KEY)
    
    @property
    def INGEST_API_KEY_IS_INSECURE_DEFAULT(self) -> bool:
        return self.INGEST_API_KEY == DEFAULT_INGEST_API_KEY
    
    # OAuth Credentials (from environment)
    GEMINI_OAUTH_CLIENT_ID: str = os.getenv("GEMINI_OAUTH_CLIENT_ID", "")
    GEMINI_OAUTH_CLIENT_SECRET: str = os.getenv("GEMINI_OAUTH_CLIENT_SECRET", "")
    
    # Quota Limits
    CLAUDE_PRO_LIMIT: int = int(os.getenv("CLAUDE_PRO_LIMIT", "2000000"))
    CLAUDE_FREE_LIMIT: int = int(os.getenv("CLAUDE_FREE_LIMIT", "500000"))
    
    # Path settings
    CLAUDE_PROJECTS_DIR: str = os.getenv("CLAUDE_PROJECTS_DIR", os.path.join(get_platform_config_dir("claude"), "projects"))
    GEMINI_SESSIONS_DIR: str = os.getenv("GEMINI_SESSIONS_DIR", os.path.join(get_platform_data_dir("gemini"), "tmp", "sessions"))
    GEMINI_OAUTH_PATH: str = os.getenv("GEMINI_OAUTH_PATH", os.path.join(get_platform_config_dir("gemini"), "oauth_creds.json"))
    CHATGPT_SESSIONS_DIR: str = os.getenv("CHATGPT_SESSIONS_DIR", os.path.join(get_platform_config_dir("codex"), "sessions"))
    ANTIGRAVITY_QUOTA_PATH: str = os.getenv("ANTIGRAVITY_QUOTA_PATH", os.path.join(get_platform_data_dir("antigravity"), "state", "quota.json"))
    OPENCODE_DB_PATH: str = os.getenv("OPENCODE_DB_PATH", os.path.join(get_platform_data_dir("opencode"), "opencode.db"))
    EXTERNAL_METRICS_PATH: str = os.getenv("EXTERNAL_METRICS_PATH", os.path.join(get_platform_config_dir("usage-tracker"), "external_metrics.json"))
    OPENCODE_LOCAL_COLLECTOR_ENABLED: bool = os.getenv("OPENCODE_LOCAL_COLLECTOR_ENABLED", "true").lower() == "true"
    
    # Network settings
    APP_HOST: str = os.getenv("APP_HOST", "127.0.0.1")  # Default: local-only for security
    APP_PORT: int = int(os.getenv("APP_PORT", "8765"))
    CORS_ORIGINS: list = ["http://localhost:8765", "http://127.0.0.1:8765"]

settings = Settings()

# Security check: Warn if using default ingest secret
if settings.INGEST_API_KEY_IS_INSECURE_DEFAULT:
    logger.warning("=" * 60)
    logger.warning("SECURITY WARNING: Using default INGEST_API_KEY ('sidecar-default-secret')")
    logger.warning("The ingest endpoint is DISABLED until a custom key is set.")
    logger.warning("Set INGEST_API_KEY environment variable to a strong secret.")
    logger.warning("=" * 60)
