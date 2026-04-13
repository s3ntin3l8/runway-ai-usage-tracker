# app/core/config.py
import logging
import os
import platform

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

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
    if system == "Darwin":
        return os.path.join(home, "Library", "Application Support", app_name)
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
    if system == "Darwin":
        return os.path.join(home, "Library", "Application Support", app_name)
    xdg_config_home = os.getenv("XDG_CONFIG_HOME")
    if xdg_config_home:
        return os.path.join(xdg_config_home, app_name)
    return os.path.join(home, ".config", app_name)


DEFAULT_INGEST_API_KEY = "sidecar-default-secret"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    PROJECT_NAME: str = "Runway — AI Limits Dashboard"
    RUN_MODE: str = "standalone"

    # GitHub OAuth
    GITHUB_CLIENT_ID: str = "Iv1.b507a08c87ecfe98"
    GITHUB_TOKEN: str = ""

    # Provider tokens
    CHATGPT_OAUTH_TOKEN: str = ""
    CLAUDE_CODE_OAUTH_TOKEN: str = ""
    OLLAMA_SESSION_TOKEN: str = ""
    OPENROUTER_API_KEY: str = ""
    MINIMAX_API_KEY: str = ""
    OPENCODE_GO_API_KEY: str = ""
    ZAI_API_KEY: str = ""
    KIMI_API_KEY: str = ""
    KIMI_AUTH_TOKEN: str = ""

    INGEST_API_KEY: str = DEFAULT_INGEST_API_KEY
    ADMIN_API_KEY: str | None = None

    # OAuth credentials
    GEMINI_OAUTH_CLIENT_ID: str = ""
    GEMINI_OAUTH_CLIENT_SECRET: str = ""
    CLAUDE_OAUTH_CLIENT_ID: str = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

    KEYCHAIN_PROMPT_MODE: str = "always"

    # Quota limits
    CLAUDE_PRO_LIMIT: int = 2000000
    CLAUDE_FREE_LIMIT: int = 500000

    # Path settings — defaults computed at class load, overrideable via env var
    CLAUDE_PROJECTS_DIR: str = Field(
        default_factory=lambda: os.path.join(get_platform_config_dir("claude"), "projects")
    )
    CLAUDE_STATUSLINE_PATH: str = Field(
        default_factory=lambda: os.path.join(get_platform_config_dir("claude"), "statusline.json")
    )
    GEMINI_SESSIONS_DIR: str = Field(
        default_factory=lambda: os.path.join(get_platform_data_dir("gemini"), "tmp", "sessions")
    )
    GEMINI_OAUTH_PATH: str = Field(
        default_factory=lambda: os.path.join(get_platform_config_dir("gemini"), "oauth_creds.json")
    )
    ANTHROPIC_OAUTH_PATH: str = Field(
        default_factory=lambda: os.path.join(get_platform_config_dir("claude"), "oauth_creds.json")
    )
    GITHUB_OAUTH_PATH: str = Field(
        default_factory=lambda: os.path.join(
            get_platform_config_dir("usage-tracker"), "github_oauth.json"
        )
    )
    CHATGPT_AUTH_PATH: str = Field(default_factory=lambda: os.path.expanduser("~/.codex/auth.json"))
    CHATGPT_SESSIONS_DIR: str = Field(
        default_factory=lambda: os.path.join(get_platform_config_dir("codex"), "sessions")
    )
    ANTIGRAVITY_QUOTA_PATH: str = Field(
        default_factory=lambda: os.path.join(
            get_platform_data_dir("antigravity"), "state", "quota.json"
        )
    )
    OPENCODE_DB_PATH: str = Field(
        default_factory=lambda: os.path.join(get_platform_data_dir("opencode"), "opencode.db")
    )
    DATABASE_PATH: str = Field(
        default_factory=lambda: os.path.join(get_platform_config_dir("usage-tracker"), "runway.db")
    )
    EXTERNAL_METRICS_PATH: str = Field(
        default_factory=lambda: os.path.join(
            get_platform_config_dir("usage-tracker"), "external_metrics.json"
        )
    )

    LOCAL_COLLECTOR_ENABLED: bool = True
    LOCAL_CREDENTIAL_SCRAPING_ENABLED: bool = True
    BROWSER_PREFERENCE: str = "safari,chrome,chromium,edge,firefox"

    # Network
    APP_HOST: str = "127.0.0.1"
    APP_PORT: int = 8765

    # Encryption
    DB_ENCRYPTION_KEY: str | None = None

    # Logging format: "plain" (default) or "json"
    LOG_FORMAT: str = "plain"

    @property
    def INGEST_API_KEY_IS_INSECURE_DEFAULT(self) -> bool:
        return self.INGEST_API_KEY == DEFAULT_INGEST_API_KEY

    @computed_field
    @property
    def DATABASE_URL(self) -> str:
        return f"sqlite:///{self.DATABASE_PATH}"

    @property
    def CORS_ORIGINS(self) -> list[str]:
        origins = os.getenv("CORS_ORIGINS")
        if origins:
            return [o.strip() for o in origins.split(",")]
        return [
            f"http://localhost:{self.APP_PORT}",
            f"http://127.0.0.1:{self.APP_PORT}",
        ]


settings = Settings()

# Security check: Warn if using default ingest secret
if settings.INGEST_API_KEY_IS_INSECURE_DEFAULT:
    logger.warning("=" * 60)
    logger.warning("SECURITY WARNING: Using default INGEST_API_KEY ('sidecar-default-secret')")
    logger.warning("The ingest endpoint is DISABLED until a custom key is set.")
    logger.warning("Set INGEST_API_KEY environment variable to a strong secret.")
    logger.warning("=" * 60)
