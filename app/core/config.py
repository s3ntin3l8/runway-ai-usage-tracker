# app/core/config.py
import logging
import os
import platform
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    if app_name == "runway":
        override = os.getenv("RUNWAY_CONFIG_DIR")
        if override:
            return override

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

    # GitHub OAuth
    GITHUB_CLIENT_ID: str = "Iv1.b507a08c87ecfe98"
    GITHUB_TOKEN: str = ""

    # Provider tokens
    CHATGPT_OAUTH_TOKEN: str = ""
    CLAUDE_CODE_OAUTH_TOKEN: str = ""
    OLLAMA_SESSION_TOKEN: str = ""
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_HTTP_REFERER: str = ""
    OPENROUTER_X_TITLE: str = "Runway"
    MINIMAX_API_KEY: str = ""
    MINIMAX_HOST: str = ""  # Override: "platform.minimaxi.com" for China
    MINIMAX_COOKIE: str = ""  # Manual cookie override for fallback
    OPENCODE_GO_API_KEY: str = ""
    ZAI_API_KEY: str = ""
    ZAI_API_HOST: str = ""  # Override: "open.bigmodel.cn" for China
    ZAI_QUOTA_URL: str = ""  # Override: full URL to quota endpoint
    KIMI_API_KEY: str = ""
    KIMI_AUTH_TOKEN: str = ""
    KIMI_K2_API_KEY: str = ""

    INGEST_API_KEY: str = ""  # Default empty = disabled; set to non-empty to enable ingestion
    ADMIN_API_KEY: str | None = None

    # Comma-separated list of reverse-proxy IPs allowed to assert auth via
    # X-Forwarded-User / Remote-User headers. Empty = proxy-header auth
    # disabled (default). Without this, anyone could forge the header.
    TRUSTED_PROXY_IPS: str = ""

    # Operator's assertion that something in front of the server (nginx,
    # caddy, cloudflare, kube ingress, etc.) terminates TLS. Required to
    # start when bound to a non-localhost interface — sidecar payloads
    # carry OAuth tokens and cookies, and HMAC protects integrity but
    # not confidentiality. Localhost binds don't need it.
    TLS_TERMINATED: bool = False

    # Lifetime of a browser session cookie minted by exchanging the admin
    # key at POST /auth/session. The "remember me" path uses the longer
    # window; the default path the shorter one. Revoking every session at
    # once is a SESSION_SECRET rotation (POST /auth/revoke-all), independent
    # of these expiries. See app/core/sessions.py.
    SESSION_LIFETIME_HOURS: int = 12
    SESSION_REMEMBER_DAYS: int = 30

    # OAuth credentials
    GEMINI_OAUTH_CLIENT_ID: str = ""
    GEMINI_OAUTH_CLIENT_SECRET: str = ""
    CLAUDE_OAUTH_CLIENT_ID: str = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

    # Background OAuth auto-refresh — scans the in-memory token cache and
    # refreshes any JWT-bearing token whose `exp` falls inside the threshold.
    # Independent from the collector's opportunistic refresh, so tokens stay
    # fresh even when the poller is dormant.
    TOKEN_AUTO_REFRESH_ENABLED: bool = True
    TOKEN_AUTO_REFRESH_INTERVAL_SECONDS: int = 300
    TOKEN_AUTO_REFRESH_THRESHOLD_SECONDS: int = 600

    KEYCHAIN_PROMPT_MODE: str = "always"

    # Quota limits
    CLAUDE_MAX_LIMIT: int = 10000000
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
    # Gemini CLI hardcodes ~/.gemini/oauth_creds.json on every platform
    # (not XDG-compliant — see github.com/google-gemini/gemini-cli
    # packages/core/src/utils/paths.ts: GEMINI_DIR = '.gemini').
    GEMINI_OAUTH_PATH: str = Field(
        default_factory=lambda: os.path.join(os.path.expanduser("~"), ".gemini", "oauth_creds.json")
    )
    ANTHROPIC_OAUTH_PATH: str = Field(
        default_factory=lambda: os.path.join(get_platform_config_dir("claude"), "oauth_creds.json")
    )
    GITHUB_OAUTH_PATH: str = Field(
        default_factory=lambda: os.path.join(get_platform_config_dir("runway"), "github_oauth.json")
    )
    CHATGPT_AUTH_PATH: str = Field(default_factory=lambda: os.path.expanduser("~/.codex/auth.json"))
    CHATGPT_SESSIONS_DIR: str = Field(
        default_factory=lambda: os.path.join(get_platform_config_dir("codex"), "sessions")
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def data_dir(self) -> str:
        return get_platform_data_dir("runway")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def config_dir(self) -> str:
        return get_platform_config_dir("runway")

    OPENCODE_DB_PATH: str = Field(
        default_factory=lambda: os.path.join(get_platform_data_dir("opencode"), "opencode.db")
    )
    DATABASE_PATH: str = Field(
        default_factory=lambda: os.path.join(get_platform_config_dir("runway"), "runway.db")
    )

    BROWSER_PREFERENCE: str = "safari,chrome,chromium,edge,firefox"

    # Network
    APP_HOST: str = "127.0.0.1"
    APP_PORT: int = 8765

    # Encryption
    DB_ENCRYPTION_KEY: str | None = None

    # Logging format: "plain" (default) or "json"
    LOG_FORMAT: str = "plain"

    # Display timezone — IANA name (e.g. "Europe/Berlin"). Standard Linux/Docker
    # env var. Used as a fallback display tz when no SystemConfig override is
    # set; the dashboard further falls back to the browser's auto-detected zone.
    TZ: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def env_timezone(self) -> str | None:
        """Validated `TZ` env var, or None if unset/invalid."""
        if not self.TZ:
            return None
        try:
            ZoneInfo(self.TZ)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning(
                "Invalid TZ env var %r — ignoring for dashboard display. "
                "Use an IANA name like 'Europe/Berlin'.",
                self.TZ,
            )
            return None
        return self.TZ

    @property
    def INGEST_API_KEY_IS_INSECURE_DEFAULT(self) -> bool:
        return self.INGEST_API_KEY == DEFAULT_INGEST_API_KEY

    @property
    def trusted_proxy_ips(self) -> set[str]:
        return {ip.strip() for ip in self.TRUSTED_PROXY_IPS.split(",") if ip.strip()}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def DATABASE_URL(self) -> str:
        return f"sqlite:///{self.DATABASE_PATH}"

    @property
    def CORS_ORIGINS(self) -> list[str]:
        origins = os.getenv("CORS_ORIGINS")
        if origins:
            return [o.strip() for o in origins.split(",")]
        # Non-localhost without explicit CORS_ORIGINS is rejected by
        # _validate_security_invariants at startup, so reaching this
        # default means APP_HOST is local.
        return [
            f"http://localhost:{self.APP_PORT}",
            f"http://127.0.0.1:{self.APP_PORT}",
        ]


def _validate_security_invariants(s: Settings) -> None:
    """Refuse to start when production-mode preconditions aren't met.

    "Production mode" = bound to a non-localhost interface. In that mode the
    server is reachable from the network and ships secrets across the wire:

    1. DB_ENCRYPTION_KEY must be set (otherwise OAuth tokens, cookies, and
       ingest API keys sit in plaintext on disk).
    2. TLS_TERMINATED must be asserted (otherwise sidecar payloads — tokens,
       cookies — travel as cleartext on the wire).
    3. CORS_ORIGINS must be explicitly set (the legacy fallback to ["*"]
       combined with allow_credentials=True is rejected by browsers and
       leaves the dashboard non-functional cross-origin).

    Also: DB_ENCRYPTION_KEY is required whenever ADMIN_API_KEY is set, since
    the admin endpoints mutate state secured by that key.

    Raises RuntimeError when any precondition fails. Localhost binds are
    exempt by design — Runway's primary topology is "developer's laptop"
    and the gates would block that flow.
    """
    if s.ADMIN_API_KEY and not s.DB_ENCRYPTION_KEY:
        logger.error(
            "SECURITY ERROR: ADMIN_API_KEY is set while DB_ENCRYPTION_KEY is not configured."
        )
        raise RuntimeError("DB_ENCRYPTION_KEY must be set when ADMIN_API_KEY is configured")

    if s.APP_HOST in ("127.0.0.1", "localhost", "::1"):
        return  # localhost binds are exempt from the multi-host gates

    if not s.DB_ENCRYPTION_KEY:
        logger.error("SECURITY ERROR: Server bound to non-localhost without DB_ENCRYPTION_KEY.")
        raise RuntimeError("DB_ENCRYPTION_KEY must be set when binding to non-localhost interfaces")
    if not s.TLS_TERMINATED:
        logger.error(
            "SECURITY ERROR: Server bound to non-localhost without TLS_TERMINATED=1. "
            "Sidecar payloads include tokens — they must not travel as cleartext."
        )
        raise RuntimeError(
            "TLS termination must be asserted (set TLS_TERMINATED=1) when binding "
            "to non-localhost interfaces. Front the server with nginx / caddy / "
            "cloudflare / kube ingress and set the flag once TLS is in place."
        )
    if not os.getenv("CORS_ORIGINS"):
        logger.error("SECURITY ERROR: Server bound to non-localhost without explicit CORS_ORIGINS.")
        raise RuntimeError(
            "CORS_ORIGINS must be set to an explicit comma-separated origin list when "
            "binding to non-localhost interfaces. The default ['*'] is rejected by "
            "browsers in combination with credentialed requests."
        )


def _warn_about_ingest_key(s: Settings) -> None:
    if not s.INGEST_API_KEY:
        logger.warning("=" * 60)
        logger.warning(
            "INGEST_API_KEY is not configured. The ingest endpoint is DISABLED until a non-empty key is provided."
        )
        logger.warning(
            "Set INGEST_API_KEY environment variable to a strong secret to enable sidecar ingestion."
        )
        logger.warning("=" * 60)
    elif s.INGEST_API_KEY_IS_INSECURE_DEFAULT:
        logger.warning("=" * 60)
        logger.warning("SECURITY WARNING: Using default INGEST_API_KEY ('sidecar-default-secret')")
        logger.warning("The ingest endpoint is DISABLED until a custom key is set.")
        logger.warning("Set INGEST_API_KEY environment variable to a strong secret.")
        logger.warning("=" * 60)


settings = Settings()
_validate_security_invariants(settings)
_warn_about_ingest_key(settings)
