import re
import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.models.schemas_cards import LimitCard

#: How long a reported git repository name may be. The name becomes a project
#: label, so it is bounded like every other label the server stores.
_PROJECT_REF_MAX_LENGTH = 128


class UsageEventPush(BaseModel):
    """One usage event pushed by a sidecar."""

    provider_id: str
    account_id: str
    event_id: str  # dedup key
    ts: str  # ISO-8601 (UTC) of the actual event
    model_id: str | None = None
    # The client app that produced the event ("opencode", "claude_code",
    # "minimax_code", ...). provider_id names the upstream provider billed,
    # which several client apps share — a canonical-provider retag would
    # otherwise erase where the traffic came from. Optional so an older
    # sidecar still pushes valid events; the server then falls back to
    # provider_id.
    source_app: str | None = None
    session_id: str | None = None
    subagent_type: str | None = None  # "Explore" | "Plan" | None for main thread
    # The provider's own turn identifier, when it has one. A provider that
    # emits one event per *request* (MiniMax Code) uses it to count turns
    # instead of requests; a provider that emits one event per message leaves
    # it unset, and the turn count falls back to the event count.
    turn_id: str | None = None
    # Working-directory / project context (enrichment). cwd = raw per-message
    # path; the server derives `project` (root basename) in EventIngestor via
    # app/services/project_label.py. tool_names = the tool_use block names in the
    # message (Anthropic), stored as a JSON array.
    cwd: str | None = None
    # The git repository the working directory belongs to, as the *name* only
    # ("runway-ai-usage-tracker") — never the remote URL, which can carry
    # credentials. The client resolves it on its own host, because the server
    # never reads the client's filesystem. Optional: an older sidecar sends
    # none, and the server then labels the row with the folder's name.
    #
    # The bound and the character filter live here as well as in the client:
    # this value ends up in the indexed, UI-facing project label, so the
    # invariant has to hold for any client, not only the one we ship.
    project_ref: str | None = Field(default=None, max_length=_PROJECT_REF_MAX_LENGTH)

    @field_validator("project_ref", mode="before")
    @classmethod
    def clean_project_ref(cls, value: Any) -> str | None:
        """A reported repository name: printable characters, bounded, not empty.

        A name never holds a path separator — a basename cannot — so a value
        that does is not a name and is dropped: it would pin a path-shaped
        label, which the `path_shaped_projects` repair then relabels back to
        the folder's name, and the two fixes would fight over the same rows.
        """
        if value is None:
            return None
        cleaned = "".join(char for char in str(value) if char.isprintable()).strip()
        if not cleaned or "/" in cleaned or "\\" in cleaned:
            return None
        return cleaned[:_PROJECT_REF_MAX_LENGTH] or None

    git_branch: str | None = None
    tool_names: list[str] | None = None
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_cache_read: int = 0
    tokens_cache_create: int = 0
    # Anthropic splits cache writes by TTL (1h writes cost 2x base input, 5m cost
    # 1.25x); tokens_cache_create stays the sum of these two so back-compat
    # readers/rollups need no change. Both 0 (old sidecars, other providers)
    # means "split unknown" and cost_calculator treats the total as all-5m.
    tokens_cache_create_1h: int = 0
    tokens_cache_create_5m: int = 0
    tokens_reasoning: int = 0
    stop_reason: str | None = None
    tool_calls: int = 0
    latency_ms: int | None = None
    raw_json: str | None = None
    # OpenCode's raw providerID (gateway/billing tier tag), forwarded by the
    # sidecar so the server can stop guessing OpenRouter traffic from the
    # model id. Optional: older sidecars send none and the server falls back
    # to the model-id shape test.
    raw_provider_id: str | None = None
    # Claude Code per-message dimensions (from JSONL, otherwise None/0 for
    # providers/older sidecars that don't emit them).
    effort: str | None = None  # "low" | "medium" | "high" | "xhigh" | "max"
    speed: str | None = None  # "fast" when fast mode was active, else "standard"
    service_tier: str | None = None  # "standard" | "priority" | "batch"
    entrypoint: str | None = None  # "cli" | "sdk-ts" | "claude-vscode" | ...
    app_version: str | None = None  # Claude Code version that logged the message
    web_search_requests: int = 0
    web_fetch_requests: int = 0
    # cost_usd: set by providers that log it directly (e.g. OpenCode); None = server computes
    # sidecar_id intentionally NOT here — comes from IngestRequest.sidecar_id
    cost_usd: float | None = None
    # Attribution decision made while reading the source logs. Missing legacy
    # values are treated as unresolved when account_id is the default sentinel.
    account_source: str | None = None  # local | tag | default
    credential_origin: str | None = Field(
        default=None, max_length=4096
    )  # directory-scoped event origin
    kind: str = "message"  # "message" | "error"; reserved: "reset", "anomaly"
    error_reason: str | None = (
        None  # short tag: "rate_limit", "auth_failed", "quota_exceeded", "timeout"
    )


class LoginDirectorySettings(BaseModel):
    claude_config_dirs: list[str] = Field(default_factory=list)
    codex_home: list[str] = Field(default_factory=list)

    @field_validator("claude_config_dirs", "codex_home", mode="before")
    @classmethod
    def directories_only(cls, value: Any) -> list[str]:
        from app.core.login_dirs import validate_directories

        return validate_directories(value)


class LoginDirectoryReport(BaseModel):
    applied: LoginDirectorySettings = Field(default_factory=LoginDirectorySettings)
    searched: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("searched")
    @classmethod
    def bounded_search_paths(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        if set(value) - {"anthropic", "chatgpt"} or any(
            len(paths) > 128
            or any(len(path) > 4096 or any(ord(c) < 32 for c in path) for path in paths)
            for paths in value.values()
        ):
            raise ValueError("Invalid searched directories")
        return value


def read_login_directory_report(value: str | None) -> LoginDirectoryReport | None:
    if not value:
        return None
    try:
        return LoginDirectoryReport.model_validate_json(value)
    except ValueError:
        return None


SESSION_NAME_MAX_LENGTH = 200
SESSION_NAMES_MAX_PER_REQUEST = 2000

_WHITESPACE_RUN = re.compile(r"\s+")
_JOINERS = frozenset("\u200c\u200d")


class SessionNameIn(BaseModel):
    """One session name reported by a sidecar (or a collector).

    ``provider_id`` is the provider the sidecar stamps on that session's events
    (the id the Sessions view filters by). ``kind`` is ``agent`` (set by the user
    or a launcher) or ``generated`` (produced by the tool). The name is free
    text that may hold sensitive wording: it is cleaned here and never logged.
    """

    provider_id: str
    session_id: str
    source_app: str | None = None
    name: str
    kind: Literal["agent", "generated"]

    @field_validator("name", mode="before")
    @classmethod
    def clean_name(cls, value: Any) -> str:
        """Control characters out, whitespace collapsed, bounded. May end up empty."""
        if value is None:
            return ""
        # Control (Cc), surrogate (Cs) and invisible format (Cf: bidi overrides,
        # ZWSP, LRM/RLM, BOM, soft hyphen) characters go. ZWJ/ZWNJ stay so emoji
        # sequences and Persian/Arabic/Indic shaping survive, but a name made of
        # nothing visible is dropped.
        text = "".join(" " if char.isspace() else char for char in str(value))
        text = "".join(
            char
            for char in text
            if (cat := unicodedata.category(char)) not in ("Cc", "Cs")
            and (cat != "Cf" or char in _JOINERS)
        )
        cleaned = _WHITESPACE_RUN.sub(" ", text).strip()[:SESSION_NAME_MAX_LENGTH]
        cleaned = cleaned.rstrip("\u200c\u200d").strip()
        return cleaned if any(char not in _JOINERS for char in cleaned) else ""


class IngestRequest(BaseModel):
    """Updated body for /api/v1/fleet/ingest."""

    provider: str
    metrics: list[LimitCard] = Field(default_factory=list)
    events: list[UsageEventPush] = Field(default_factory=list)  # NEW
    sidecar_id: str | None = None  # Originating host identifier (Phase 4B fleet mgmt)
    sidecar_version: str | None = None  # App version from package.json
    os_platform: str | None = None  # platform.system() + "/" + platform.release()
    # Whether this build can self-update in place (frozen, non-Docker). None for
    # sidecars that don't report it yet → server stays permissive.
    self_update_capable: bool | None = None
    # Whether the sidecar runs with --keep-alive. None from older sidecars → "unknown".
    keep_alive: bool | None = None
    # Per sidecar-run login (provider_id -> running); None from sidecars that predate it.
    keep_alive_providers: dict[str, bool] | None = None
    # Effective auto-update setting and update channel (local config beats the dashboard);
    # None from sidecars that predate them.
    auto_update: bool | None = None
    update_channel: str | None = None
    # Level the sidecar logs at now (config.json, or the dashboard's override); None from older ones.
    log_level: str | None = None
    login_dirs_report: LoginDirectoryReport | None = None
    collection_errors: int = 0  # Number of provider collection failures in this cycle
    # Providers with a complete local-card snapshot in this payload. None from
    # older sidecars means upsert-only; [] is a complete empty report.
    completed_providers: list[str] | None = None
    last_log_lines: list[str] = Field(default_factory=list)
    # provider_id → {"account_id", "source"}: which account this sidecar
    # stamped each event provider's data with this cycle, and why
    # ("local" | "tag" | "default"). None from sidecars that don't report it.
    identity_sources: dict[str, dict[str, str]] | None = None
    # Session names (display titles). Optional: an older sidecar sends none, and
    # an older server ignores the field. Empty names are dropped by the server.
    session_names: list[SessionNameIn] | None = None
    # api_key is now passed via X-Signature header for security
    # NOTE: deltas field removed — replaced by events[]

    @field_validator("session_names", mode="before")
    @classmethod
    def lenient_session_names(cls, value: Any) -> list[SessionNameIn] | None:
        """Names are best-effort: a malformed or surplus entry is dropped, never
        a reason to reject the payload (its events must still be ingested)."""
        if value is None:
            return None
        if not isinstance(value, list):
            return None
        kept: list[SessionNameIn] = []
        for item in value[:SESSION_NAMES_MAX_PER_REQUEST]:
            try:
                entry = (
                    item if isinstance(item, SessionNameIn) else SessionNameIn.model_validate(item)
                )
            except ValueError:
                continue
            if entry.name and entry.session_id and entry.provider_id:
                kept.append(entry)
        return kept
