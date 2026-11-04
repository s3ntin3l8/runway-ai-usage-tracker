from pydantic import BaseModel, Field

from app.models.schemas_ingest import LoginDirectoryReport

# --- Credential inventory (`GET /system/credentials`) ---------------------------------
# One row per credential *source* (a secret found in one place), grouped
# provider → account. Never carries secret values — only types, origin and health.


class CredentialSourceView(BaseModel):
    source_id: str
    provider_id: str
    account_id: str  # the storage key; may be a placeholder while identity is pending
    # "machine" = a sidecar found it, "config" = pasted in Settings → Providers,
    # "server" = env var / file on the server host.
    origin_kind: str
    origin_type: str  # file | env | cookie | keychain | sidecar (unrecognised origin) | config ...
    label: str  # file name, env var, "Browser cookie", "Manual configuration"
    origin_app: str | None = None  # tool that owns the file: "Codex CLI", "OpenCode", ...
    origin_path: str | None = None  # file path with the home directory collapsed to "~"
    # Extra context for the origin: the browser and profile a cookie came from
    # ("Chrome · Profile 1"), the keychain service, ... None when there is none.
    origin_detail: str | None = None
    login_hint: str | None = None  # how to re-authenticate the owning tool
    machine_id: str | None = None
    machine_name: str | None = None
    machine_stale: bool = False  # the machine has stopped checking in (Fleet's rule)
    # Other machines holding the same secret (matched by fingerprint; never returned itself).
    shared_with: list[str] = Field(default_factory=list)
    # The subset of ``shared_with`` whose machine has stopped checking in: it can't renew
    # anything, so it is no reason to warn about the login being copied.
    shared_with_stale: list[str] = Field(default_factory=list)
    # Why this credential belongs to its account: local (identity read on the machine),
    # verified (server resolved it), claim (sidecar claimed it), operator (tag),
    # config, server, or pending.
    mapping: str
    mapping_scope: str | None = None  # "machine" | "all_machines" for tags
    fingerprinted: bool = False  # origin carries a credential fingerprint
    identity_pending: bool = False
    # valid | expiring | expired | invalid | stale | unknown
    status: str
    expires_at: str | None = None
    expires_in_seconds: int | None = None
    token_types: list[str] = Field(default_factory=list)
    can_refresh: bool = False  # the server can refresh it on demand (the Refresh action)
    rollable: bool = False  # carries a refresh token that something renews before it lapses
    # Who renews it: "server", or "machine" (a rotating provider's login owned by a
    # machine's CLI, which the server must not refresh); None when nothing does.
    refreshed_by: str | None = None
    # Sidecar keep-alive on the machine that owns this login: "on" / "off" / "unknown"
    # (older sidecar). None when it doesn't apply (not a machine-sourced login of a KEEP_ALIVE_PROVIDERS provider
    # that carries a refresh credential).
    keep_alive: str | None = None
    # The operator's server-side override for that sidecar's keep-alive (``keep_alive`` above
    # is what it actually runs): True/False = requested on/off, None = no override. Present on
    # the same rows as ``keep_alive``; the UI compares the two to show a pending request.
    keep_alive_desired: bool | None = None
    # The provider rejected this credential (HTTP 401/403 at the last collection, or an
    # in-memory rejection flag matches its identity).
    rejected: bool = False
    # Expired and unrefreshable, but another healthy credential for the same account can stand
    # in for it: not blocking collection, so it should not raise a hard dashboard alert.
    redundant: bool = False
    removable: bool = False
    enabled: bool = True
    priority: int = 0
    live: bool = False  # a secret bundle is currently held in the server cache
    is_active: bool = False  # produced this account's most recent successful collection
    health: str = "untried"  # untried | healthy | degraded | auth_failed | unavailable
    last_seen: str | None = None
    last_attempt_at: str | None = None
    last_success_at: str | None = None
    last_error: str | None = None
    # Why a server env/file credential that is *present* is not feeding any collection:
    # provider_disabled | default_disabled | account_keyed_config | shadowed_by_config_key.
    unused_reason: str | None = None


class CredentialAccountView(BaseModel):
    provider_id: str
    account_id: str
    account_label: str | None = None
    status: str  # the best status among enabled sources
    identity_pending: bool = False
    active_source_id: str | None = None
    data_source: str | None = None  # from the account's latest quota card
    input_source: str | None = None
    sources: list[CredentialSourceView] = Field(default_factory=list)
    # The account is retired: its ``provider_configs`` row is archived, so
    # nothing is collected for it. A credential mapped onto it is *parked*
    # (see docs/api-reference.md#parked-mappings).
    archived: bool = False


class CredentialProviderView(BaseModel):
    provider_id: str
    name: str
    accounts: list[CredentialAccountView] = Field(default_factory=list)


class CredentialMachineView(BaseModel):
    login_dirs_report: LoginDirectoryReport | None = None
    machine_id: str
    name: str
    last_seen: str | None = None
    credential_count: int = 0
    unmapped_count: int = 0
    stale: bool = False  # not seen within the Fleet stale threshold


class BlockedCollectionView(BaseModel):
    """A credential a machine found but could not map to an account, whose provider is not
    collecting: the quota behind it stays dark until someone assigns an account."""

    sidecar_id: str
    provider_id: str
    credential_origin: str
    first_seen: str | None = None


class BlockedSourceView(BaseModel):
    """A credential source a machine found but could not read.

    The provider is not "not signed in" - its credential exists, the sidecar just cannot
    read it (a browser holds the cookie store open, or the values are app-bound
    encrypted). ``collecting`` says whether another source covers the provider anyway.
    """

    sidecar_id: str
    provider_id: str
    credential_origin: str
    source_store: str | None = None
    reason: str
    detail: str | None = None
    remedy: str | None = None
    collecting: bool = False
    first_seen: str | None = None
    last_seen: str | None = None


class AffectedCredentialProvider(BaseModel):
    provider_id: str
    name: str


class CredentialFileMigrationNotice(BaseModel):
    providers: list[AffectedCredentialProvider]
    removed_count: int
    recorded_at: str


class MergedAccountView(BaseModel):
    """An account that was merged away, and the account it became.

    The merged-away account holds no credential any more (the merge moved it),
    so it cannot appear among the inventory's accounts. This is what tells a
    client "this account no longer exists" from "this account never existed".
    """

    provider_id: str
    account_id: str  # the account that was merged away
    destination_account_id: str  # where it ended up (a chain is followed)
    merged_at: str


class CredentialInventory(BaseModel):
    credential_file_migration_notice: CredentialFileMigrationNotice | None = None
    providers: list[CredentialProviderView] = Field(default_factory=list)
    machines: list[CredentialMachineView] = Field(default_factory=list)
    unmapped_count: int = 0  # credentials awaiting an account (Fleet "untagged")
    rule_count: int = 0  # operator mapping rules
    pending_usage_events: int = 0  # collected events with no account yet
    # Unmapped credentials whose provider has no fresh collection anywhere (Home banner).
    blocked_collection: list[BlockedCollectionView] = Field(default_factory=list)
    # Credential sources a machine found but could not read (a locked or app-bound
    # browser cookie store). Kept out of `unmapped_count`: there is no credential to
    # map, the source simply cannot be read.
    blocked_sources: list[BlockedSourceView] = Field(default_factory=list)
    # The accounts that were merged away, and where each of them went.
    merged_accounts: list[MergedAccountView] = Field(default_factory=list)
