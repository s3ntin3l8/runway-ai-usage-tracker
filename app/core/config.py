import os
import json
import logging
from typing import Optional
import platform
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
    RUN_MODE: str = os.getenv(
        "RUN_MODE", "standalone"
    )  # "standalone", "multi-host", "docker"

    # GitHub OAuth Settings
    GITHUB_CLIENT_ID: str = os.getenv(
        "GITHUB_CLIENT_ID", "Iv1.b507a08c87ecfe98"
    )  # VS Code official ID (trusted for Device Flow)
    GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")

    # ChatGPT OAuth Settings
    CHATGPT_OAUTH_TOKEN: str = os.getenv("CHATGPT_OAUTH_TOKEN", "")

    # Claude OAuth Settings
    CLAUDE_CODE_OAUTH_TOKEN: str = os.getenv("CLAUDE_CODE_OAUTH_TOKEN", "")

    OLLAMA_SESSION_TOKEN: str = os.getenv("OLLAMA_SESSION_TOKEN", "")
    OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
    MINIMAX_API_KEY: str = os.getenv("MINIMAX_API_KEY", "")
    OPENCODE_GO_API_KEY: str = os.getenv("OPENCODE_GO_API_KEY", "")
    ZAI_API_KEY: str = os.getenv("ZAI_API_KEY", "")
    KIMI_API_KEY: str = os.getenv("KIMI_API_KEY", "")
    KIMI_AUTH_TOKEN: str = os.getenv("KIMI_AUTH_TOKEN", "")
    INGEST_API_KEY: str = os.getenv("INGEST_API_KEY", DEFAULT_INGEST_API_KEY)
    # Optional key to protect mutation endpoints (PATCH/DELETE sidecars, token refresh).
    # If unset, mutation endpoints are open (local-first default).
    ADMIN_API_KEY: Optional[str] = os.getenv("ADMIN_API_KEY") or None

    @property
    def INGEST_API_KEY_IS_INSECURE_DEFAULT(self) -> bool:
        return self.INGEST_API_KEY == DEFAULT_INGEST_API_KEY

    # OAuth Credentials (from environment)
    GEMINI_OAUTH_CLIENT_ID: str = os.getenv("GEMINI_OAUTH_CLIENT_ID", "")
    GEMINI_OAUTH_CLIENT_SECRET: str = os.getenv("GEMINI_OAUTH_CLIENT_SECRET", "")
    CLAUDE_OAUTH_CLIENT_ID: str = os.getenv("CLAUDE_OAUTH_CLIENT_ID", "9d1c250a-e61b-44d9-88ed-5944d1962f5e")

    # Keychain prompts (always, never)
    KEYCHAIN_PROMPT_MODE: str = os.getenv("KEYCHAIN_PROMPT_MODE", "always")

    # Quota Limits
    CLAUDE_PRO_LIMIT: int = int(os.getenv("CLAUDE_PRO_LIMIT", "2000000"))
    CLAUDE_FREE_LIMIT: int = int(os.getenv("CLAUDE_FREE_LIMIT", "500000"))

    # Path settings
    CLAUDE_PROJECTS_DIR: str = os.getenv(
        "CLAUDE_PROJECTS_DIR",
        os.path.join(get_platform_config_dir("claude"), "projects"),
    )
    CLAUDE_STATUSLINE_PATH: str = os.getenv(
        "CLAUDE_STATUSLINE_PATH",
        os.path.join(get_platform_config_dir("claude"), "statusline.json"),
    )
    GEMINI_SESSIONS_DIR: str = os.getenv(
        "GEMINI_SESSIONS_DIR",
        os.path.join(get_platform_data_dir("gemini"), "tmp", "sessions"),
    )
    GEMINI_OAUTH_PATH: str = os.getenv(
        "GEMINI_OAUTH_PATH",
        os.path.join(get_platform_config_dir("gemini"), "oauth_creds.json"),
    )
    ANTHROPIC_OAUTH_PATH: str = os.getenv(
        "ANTHROPIC_OAUTH_PATH",
        os.path.join(get_platform_config_dir("claude"), "oauth_creds.json"),
    )
    GITHUB_OAUTH_PATH: str = os.getenv(
        "GITHUB_OAUTH_PATH",
        os.path.join(get_platform_config_dir("usage-tracker"), "github_oauth.json"),
    )
    CHATGPT_AUTH_PATH: str = os.path.expanduser("~/.codex/auth.json")
    CHATGPT_SESSIONS_DIR: str = os.getenv(
        "CHATGPT_SESSIONS_DIR",
        os.path.join(get_platform_config_dir("codex"), "sessions"),
    )
    ANTIGRAVITY_QUOTA_PATH: str = os.getenv(
        "ANTIGRAVITY_QUOTA_PATH",
        os.path.join(get_platform_data_dir("antigravity"), "state", "quota.json"),
    )
    OPENCODE_DB_PATH: str = os.getenv(
        "OPENCODE_DB_PATH",
        os.path.join(get_platform_data_dir("opencode"), "opencode.db"),
    )
    # Database & Persistence Settings
    DATABASE_PATH: str = os.getenv(
        "DATABASE_PATH",
        os.path.join(get_platform_config_dir("usage-tracker"), "runway.db"),
    )
    DATABASE_URL: str = f"sqlite:///{DATABASE_PATH}"
    DB_ENCRYPTION_KEY: Optional[str] = os.getenv("DB_ENCRYPTION_KEY")

    EXTERNAL_METRICS_PATH: str = os.getenv(
        "EXTERNAL_METRICS_PATH",
        os.path.join(get_platform_config_dir("usage-tracker"), "external_metrics.json"),
    )
    LOCAL_COLLECTOR_ENABLED: bool = (
        os.getenv("LOCAL_COLLECTOR_ENABLED", "true").lower() == "true"
    )
    LOCAL_CREDENTIAL_SCRAPING_ENABLED: bool = (
        os.getenv("LOCAL_CREDENTIAL_SCRAPING_ENABLED", "true").lower() == "true"
    )
    BROWSER_PREFERENCE: str = os.getenv(
        "BROWSER_PREFERENCE", "safari,chrome,chromium,edge,firefox"
    )

    # Network settings
    APP_HOST: str = os.getenv(
        "APP_HOST", "127.0.0.1"
    )  # Default: local-only for security
    APP_PORT: int = int(os.getenv("APP_PORT", "8765"))

    @property
    def CORS_ORIGINS(self) -> list:
        origins = os.getenv("CORS_ORIGINS")
        if origins:
            return [o.strip() for o in origins.split(",")]
        return [f"http://localhost:{self.APP_PORT}", f"http://127.0.0.1:{self.APP_PORT}"]


settings = Settings()

# Security check: Warn if using default ingest secret
if settings.INGEST_API_KEY_IS_INSECURE_DEFAULT:
    logger.warning("=" * 60)
    logger.warning(
        "SECURITY WARNING: Using default INGEST_API_KEY ('sidecar-default-secret')"
    )
    logger.warning("The ingest endpoint is DISABLED until a custom key is set.")
    logger.warning("Set INGEST_API_KEY environment variable to a strong secret.")
    logger.warning("=" * 60)
