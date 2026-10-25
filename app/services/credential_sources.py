"""Persistence helpers for non-secret credential source metadata."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, and_, col, or_, select

from app.models.db import (
    CredentialSource,
    CredentialTag,
    LatestUsage,
    ProviderAccountLabel,
    ProviderConfig,
    UsageEvent,
)
from app.services.account_identity import canonical_account_id
from app.services.credential_tags import retry_backoff


@dataclass(frozen=True)
class OriginDisplay:
    """UI-safe description of a credential origin (never a secret)."""

    kind: str  # env | file | cookie | keychain | sidecar
    label: str  # "auth.json", "OPENROUTER_API_KEY", "Browser cookie"
    app: str | None = None  # the tool that owns the file: "Codex CLI", "OpenCode" ...
    path: str | None = None  # display path with the home directory collapsed to "~"
    detail: str | None = None  # extra context: the browser + profile a cookie came from


# Owning tool by the file's last one or two path segments. Segments are lowercased with one
# leading "." stripped, so ``~/.codex/auth.json``, ``~/.config/codex/auth.json``,
# ``AppData\Roaming\codex\auth.json`` and ``~/Library/Application Support/codex/auth.json``
# all match ``codex/auth.json``. Paths come from app/core/registry.json and the sidecar overlay.
_FILE_APPS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("codex", "auth.json"), "Codex CLI"),
    (("opencode", "auth.json"), "OpenCode"),
    (("claude", "credentials.json"), "Claude Code"),
    (("claude", "oauth_creds.json"), "Claude Code"),
    (("claude", "statusline.json"), "Claude Code"),
    (("gemini", "oauth_creds.json"), "Gemini CLI"),
    (("antigravity-cli", "antigravity-oauth-token"), "Antigravity CLI"),
    (("antigravity", "state", "quota.json"), "Antigravity"),
    (("gh", "hosts.yml"), "GitHub CLI"),
    (("github cli", "hosts.yml"), "GitHub CLI"),
    (("runway", "github_oauth.json"), "Runway"),
    (("github-copilot", "apps.json"), "Copilot editor sign-in"),
    (("github-copilot", "hosts.json"), "Copilot editor sign-in"),
    (("copilot", "config.json"), "Copilot CLI"),
    (("grok", "auth.json"), "Grok CLI"),
    (("kimi", "config.json"), "Kimi CLI"),
    (("k2", "tokens.json"), "Kimi K2"),
    # The MiniMax Code desktop app stores its OAuth bundle under the OAuth
    # client id, so the directory names the client, not the app.
    (("mcode-public", "auth.json"), "MiniMax Code"),
    # Last resort, for a login under a user-chosen dir (``CLAUDE_CONFIG_DIR=/data/work``).
    # Only filenames no other tool uses; ``auth.json`` is shared (Codex/OpenCode/Grok).
    (("credentials.json",), "Claude Code"),
)

# Owning tool by the OS credential entry it keeps its login in. The key is the
# origin's tail - the Keychain service and, where the store needs one, the
# account: "gemini:antigravity" for the Antigravity CLI, which keeps the same
# credential in the macOS login Keychain and in the Windows Credential Manager.
_KEYCHAIN_ENTRY_APPS: dict[str, str] = {
    "gemini:antigravity": "Antigravity CLI",
}

_LOGIN_HINTS = {
    "Claude Desktop": "sign in again in Claude Desktop",
    "Codex CLI": "run `codex login`",
    "OpenCode": "run `opencode auth login`",
    "GitHub CLI": "run `gh auth login`",
    "Copilot editor sign-in": "sign in again to Copilot in your editor",
    "Copilot CLI": "run `copilot login`",
    "Claude Code": "run `claude`, then `/login`",
    "Gemini CLI": "run `gemini`, then `/auth`",
    "Grok CLI": "sign in again in the Grok CLI",
    "Antigravity CLI": "sign in again in the Antigravity CLI",
    "MiniMax Code": "sign in again in the MiniMax Code app",
}


def login_hint(app: str | None) -> str | None:
    """How to re-authenticate the tool that owns a credential file, if we know."""
    return _LOGIN_HINTS.get(app) if app else None


def _segments(path: str) -> list[str]:
    return [seg for seg in path.replace("\\", "/").split("/") if seg]


def _app_for_path(path: str) -> str | None:
    segs = [seg.lower().removeprefix(".") for seg in _segments(path)]
    if len(segs) >= 3 and segs[-3:-1] == ["kimi-code", "credentials"]:
        return "Kimi Code"
    for tail, app in _FILE_APPS:
        if len(segs) >= len(tail) and tuple(segs[-len(tail) :]) == tail:
            return app
    return None


_HOME_PREFIXES = (
    re.compile(r"^/var/home/[^/]+(?=/|$)"),
    re.compile(r"^/mnt/[a-z]/Users/[^/]+(?=/|$)", re.IGNORECASE),
    re.compile(r"^/home/[^/]+(?=/|$)"),
    re.compile(r"^/Users/[^/]+(?=/|$)"),
    re.compile(r"^/root(?=/|$)"),
    re.compile(r"^[A-Za-z]:[\\/]+Users[\\/]+[^\\/]+(?=[\\/]|$)", re.IGNORECASE),
)


def _collapse_home(path: str) -> str:
    for pattern in _HOME_PREFIXES:
        if pattern.match(path):
            return pattern.sub("~", path, count=1)
    return path


def describe_origin_full(origin: str | None) -> OriginDisplay:
    """Describe a sidecar credential origin: kind, label, owning app and a display path."""
    value = origin or "sidecar"
    if value.startswith("env:"):
        return OriginDisplay("env", value.removeprefix("env:").split("#", 1)[0])
    if value.startswith(("path:", "file:")):
        raw = value.split(":", 1)[1].split("#", 1)[0]
        raw = re.sub(r"^/{2,}(?:localhost)?(?=/)", "", raw)  # file:///home/u -> /home/u
        raw = re.sub(r"^/(?=[A-Za-z]:)", "", raw)  # file:///C:/Users -> C:/Users
        segs = _segments(raw)
        return OriginDisplay(
            "file",
            segs[-1] if segs else "Credential file",
            app=_app_for_path(raw),
            path=_collapse_home(raw) if raw else None,
        )
    if value.startswith("cookie:"):
        # Native acceptance covers these clients (#672); optional browser labels
        # and their regression cases must accompany acceptance in #683.
        parts = value.split("#", 1)[0].split("/")
        if (
            len(parts) >= 4
            and parts[0] in {"cookie:anthropic", "cookie:anthropic_console"}
            and parts[1] in {"Chrome", "Safari", "Claude Desktop"}
        ):
            # The client is named; the private profile and the fingerprint are not.
            return OriginDisplay("cookie", "Session cookie", app=parts[1])
        return OriginDisplay(
            "cookie",
            "Browser cookie",
            app="Browser",
            detail=cookie_origin_detail(value),
        )
    if value.startswith("keychain:"):
        service = value.split(":", 1)[1].split("#", 1)[0]
        if service.startswith("Claude Desktop/oauth/"):
            # The label names the app, so the composition never repeats it.
            return OriginDisplay("keychain", "Claude Desktop OAuth", app="Claude Desktop")
        app = _KEYCHAIN_ENTRY_APPS.get(service) or (
            "Claude Code" if service.startswith("Claude Code") else None
        )
        # The app name already identifies the entry ("Claude Code - Keychain
        # entry"); only an unnamed owner needs the service spelled out.
        return OriginDisplay(
            "keychain",
            "Keychain entry",
            app=app,
            detail=None if app else service or None,
        )
    return OriginDisplay("sidecar", "Sidecar credential")


def describe_origin(origin: str | None) -> tuple[str, str]:
    """Return a UI-safe source kind and label for a sidecar credential origin."""
    display = describe_origin_full(origin)
    if display.kind == "cookie" and display.label == "Session cookie" and display.app:
        return display.kind, f"{display.app} cookie"
    return display.kind, display.label


# Browsers whose cookie jar a sidecar can read. A cookie origin names the jar it
# came from ("cookie:opencode/Chrome/Profile 1/Cookies"); the generic one
# ("cookie:opencode/session") does not, and then only the site is known.
_BROWSER_TOKENS = (
    "chrome",
    "chromium",
    "firefox",
    "safari",
    "edge",
    "brave",
    "arc",
    "opera",
    "vivaldi",
    "librewolf",
    "desktop",
)


def _is_cookie_store(part: str) -> bool:
    """Whether a path segment names the store itself rather than a profile.

    Chromium keeps the cookie file in a ``Network`` folder on some platforms
    (``<profile>/Network/Cookies``), so that segment belongs to the store path
    and must never be read as a profile name.
    """
    lowered = part.lower()
    return (
        lowered.startswith("cookies") or lowered.endswith("cookies.sqlite") or lowered == "network"
    )


def cookie_origin_detail(origin: str | None) -> str | None:
    """Where a cookie credential came from: the browser and profile that hold it.

    A cookie origin reads ``cookie:<provider>/<browser>/<profile>/<store>`` when the
    machine could name the jar, and ``cookie:<provider>/session`` when it could not.
    The first part is the provider, so only the segments after it are inspected.

    An Anthropic cookie origin carries a private browser profile (#672): it is named
    by its client instead, so no profile is ever read out of it.
    """
    if not origin or not origin.startswith("cookie:"):
        return None
    rest = origin.removeprefix("cookie:").split("#", 1)[0]
    parts = [part for part in rest.split("/") if part]
    if parts and parts[0] in {"anthropic", "anthropic_console"}:
        return None
    for index, part in enumerate(parts[1:], start=1):
        lowered = part.lower()
        if not any(token in lowered for token in _BROWSER_TOKENS):
            continue
        following = parts[index + 1] if index + 1 < len(parts) else None
        profile = following if following and not _is_cookie_store(following) else None
        return " · ".join([part, profile]) if profile else part
    return None


def pending_cache_slot(provider_id: str, source_id: str) -> str:
    """The account a source is filed under while its identity is unknown.

    Anthropic keys a pending bundle by its stable source id (a rotating token must not spawn
    orphan rows); every other provider files it under ``default``. Ingest and the identity
    handlers (verification, cookie-switch revoke) must agree, so they all ask here.
    """
    return source_id if provider_id in ("anthropic", "anthropic_console") else "default"


def is_machine_bound_origin(origin: str | None) -> bool:
    """A credential that only exists on one machine: a browser's cookie jar or a keychain.

    Such an origin is the same string on every host, so a deployment-wide ("All
    machines") tag on it would follow an account switch on a machine it was never made
    for. Path and env origins are different: a shared home directory really is one origin.
    """
    return bool(origin) and str(origin).startswith(("cookie:", "keychain:"))


def retire_unkeyed_origin(
    session: Session, *, provider_id: str, sidecar_id: str, origin: str
) -> list[CredentialSource]:
    """Delete the plain-origin row superseded by *origin*'s key-scoped form.

    When a provider's origins start carrying a credential fingerprint
    (``env:ZAI_API_KEY`` becomes ``env:ZAI_API_KEY#<fp>``), the same credential is
    reported under a new source id and the old row would sit there stale. The source id
    is derived from machine and origin, so the superseded one is computable. Returns the
    deleted rows so the caller can drop their cached bundles.
    """
    from app.services.account_identity import split_keyed_origin

    base, fingerprint = split_keyed_origin(origin)
    if fingerprint is None:
        return []
    old_source_id = sidecar_source_id(sidecar_id, base)
    rows = list(
        session.exec(
            select(CredentialSource).where(
                CredentialSource.provider_id == provider_id,
                CredentialSource.source_id == old_source_id,
                CredentialSource.sidecar_id == sidecar_id,
            )
        ).all()
    )
    for row in rows:
        session.delete(row)
    return rows


# Providers whose one login at a given path/env var re-keys itself: the credential
# behind ``base`` is replaced, never joined by a sibling, so a different
# ``base#<fp>`` from the same machine means the old fingerprint is dead. xAI rotates
# its refresh token on every refresh (#523); a provider holding several distinct
# keys under one path must not be listed — they would delete each other every cycle.
SUPERSEDING_KEYED_PROVIDERS: frozenset[str] = frozenset({"xai"})


def retire_superseded_keyed_origins(
    session: Session, *, provider_id: str, sidecar_id: str, origin: str
) -> list[CredentialSource]:
    """Delete the rows (and rotation tags) of earlier ``base#<old fp>`` origins.

    For a provider in :data:`SUPERSEDING_KEYED_PROVIDERS`, a key-scoped *origin* replaces
    every other ``base#<fp>`` the same machine reported before it: the old fingerprint
    belongs to a credential that no longer exists, so its source row would sit there
    stale and its ``rotation`` tag (written by the rotation carry, #474) would pile up.
    Operator tags are left alone — the stale-rules check surfaces those. Returns the
    deleted source rows so the caller can drop their cached bundles.
    """
    from app.services.account_identity import split_keyed_origin

    if provider_id not in SUPERSEDING_KEYED_PROVIDERS:
        return []
    base, fingerprint = split_keyed_origin(origin)
    if fingerprint is None:
        return []
    prefix = f"{base}#"

    def superseded(candidate: str | None) -> bool:
        return bool(candidate) and candidate != origin and str(candidate).startswith(prefix)

    rows = [
        row
        for row in session.exec(
            select(CredentialSource).where(
                CredentialSource.provider_id == provider_id,
                CredentialSource.sidecar_id == sidecar_id,
            )
        ).all()
        if superseded(row.credential_origin)
    ]
    for row in rows:
        session.delete(row)
    for tag in session.exec(
        select(CredentialTag).where(
            CredentialTag.provider_id == provider_id,
            CredentialTag.sidecar_id == sidecar_id,
            CredentialTag.set_by == "rotation",
        )
    ).all():
        if superseded(tag.credential_origin):
            session.delete(tag)
    return rows


def sidecar_source_id(sidecar_id: str, origin: str | None) -> str:
    """Stable source identity, scoped to the machine that reported it."""
    digest = hashlib.sha256(f"{sidecar_id}\0{origin or 'legacy'}".encode()).hexdigest()[:24]
    return f"sidecar:{digest}"


def is_sidecar_source(source: Mapping[str, Any]) -> bool:
    """Whether a credential source was reported by a sidecar.

    ``source_type`` describes the credential itself (file, env, cookie), not
    where it was observed. Sidecar ownership is established by its id and
    origin metadata.
    """
    return bool(source.get("sidecar_id") and source.get("credential_origin"))


# Sentinel: "the caller has no opinion" — distinct from an explicit ``None``
# (a credential that genuinely has no expiry).
UNSET: Any = object()


def resolve_source_account(session: Session, provider_id: str, source_id: str) -> str | None:
    """The account an already-registered source belongs to, or ``None`` if unknown.

    A ``source_id`` (sidecar + origin) names exactly one credential, so it maps to one
    account. Prefer a real identity over the ``default`` placeholder, and the most
    recently seen row when several exist.
    """
    rows = session.exec(
        select(CredentialSource)
        .where(
            CredentialSource.provider_id == provider_id,
            CredentialSource.source_id == source_id,
        )
        .order_by(col(CredentialSource.last_seen).desc())
    ).all()
    for row in rows:
        if row.account_id != "default":
            return row.account_id
    return rows[0].account_id if rows else None


def touch_source(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    source_id: str,
    source_type: str,
    source_label: str,
    credential_origin: str | None = None,
    sidecar_id: str | None = None,
    credential_expires_at: datetime | None = UNSET,
    token_types: list[str] | None = UNSET,
) -> CredentialSource:
    """Create or refresh a source without replacing operator preferences.

    Initial priority is assigned only when creating a row. Later refreshes
    preserve the operator's enabled state and priority. Refreshing metadata does
    not reset health; only a collection result confirms credential health.

    ``credential_expires_at`` / ``token_types`` are overwritten only when the caller
    passes them (``None`` is a real value: "no expiry"). A caller that doesn't know
    them — ``/fleet/ingest`` sees the secrets but the manifest reports the health
    metadata — must not wipe what the other recorded.
    """
    aid = canonical_account_id(account_id)
    row = session.exec(
        select(CredentialSource).where(
            CredentialSource.provider_id == provider_id,
            CredentialSource.account_id == aid,
            CredentialSource.source_id == source_id,
        )
    ).first()
    if row is None:
        count = session.exec(
            select(CredentialSource).where(
                CredentialSource.provider_id == provider_id,
                CredentialSource.account_id == aid,
            )
        ).all()
        # New discovered sources take the next slot so they follow existing
        # operator-configured and sidecar sources unless config claims slot 0.
        priority = max((item.priority for item in count), default=-1) + 1
        if source_type == "config":
            for item in count:
                item.priority += 1
                session.add(item)
            priority = 0
        row = CredentialSource(
            provider_id=provider_id,
            account_id=aid,
            source_id=source_id,
            source_type=source_type,
            source_label=source_label,
            credential_origin=credential_origin,
            sidecar_id=sidecar_id,
            credential_expires_at=None if credential_expires_at is UNSET else credential_expires_at,
            token_types_json=json.dumps([] if token_types is UNSET else token_types or []),
            priority=priority,
        )
        session.add(row)
    else:
        row.source_type = source_type
        row.source_label = source_label
        row.credential_origin = credential_origin
        row.sidecar_id = sidecar_id
        if credential_expires_at is not UNSET:
            row.credential_expires_at = credential_expires_at
        if token_types is not UNSET:
            row.token_types_json = json.dumps(token_types or [])
        row.last_seen = datetime.now(UTC)
    session.flush()
    return row


def account_sources(session: Session, provider_id: str, account_id: str) -> list[CredentialSource]:
    return list(
        session.exec(
            select(CredentialSource)
            .where(
                CredentialSource.provider_id == provider_id,
                CredentialSource.account_id == canonical_account_id(account_id),
            )
            .order_by(col(CredentialSource.priority), col(CredentialSource.id))
        ).all()
    )


def server_source_id(provider_id: str, source_type: str, label: str) -> str:
    """Stable id for a credential the server host itself discovered (env var / file)."""
    return f"server:{provider_id}:{source_type}:{label}"


def is_server_source_id(source_id: str) -> bool:
    return source_id.startswith("server:")


def register_server_source(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    source_type: str,
    label: str,
    token_types: list[str] | None = None,
) -> CredentialSource:
    """Record a server-discovered credential as a real source row.

    ``credential_origin`` stays ``None`` on purpose: origins are what operator tags and
    sidecar moves match on, and a server env var is not a sidecar origin. If the same
    source was previously filed under the ``default`` placeholder (identity unresolved
    then), it follows the resolved account instead of duplicating. The reverse never
    happens: a ``default``-keyed registration adopts an existing resolved row.
    """
    source_id = server_source_id(provider_id, source_type, label)
    aid = canonical_account_id(account_id)
    if aid == "default":
        # An unresolved collection (identity not obtained this cycle) must not displace the
        # row a resolved one registered: reuse it, so its health and last_success_at survive
        # and the row doesn't flip between accounts while identity resolution flaps.
        known = resolve_source_account(session, provider_id, source_id)
        if known is not None and known != "default":
            aid = known
    if aid != "default":
        stale = session.exec(
            select(CredentialSource).where(
                CredentialSource.provider_id == provider_id,
                CredentialSource.account_id == "default",
                CredentialSource.source_id == source_id,
            )
        ).first()
        target = session.exec(
            select(CredentialSource).where(
                CredentialSource.provider_id == provider_id,
                CredentialSource.account_id == aid,
                CredentialSource.source_id == source_id,
            )
        ).first()
        if stale is not None and target is None:
            stale.account_id = aid
            session.add(stale)
        elif stale is not None:
            session.delete(stale)
        session.flush()
    row = touch_source(
        session,
        provider_id=provider_id,
        account_id=aid,
        source_id=source_id,
        source_type=source_type,
        source_label=label,
        token_types=token_types if token_types is not None else UNSET,
    )
    # One env var / file is one credential: if the account it resolves to changed (a
    # rotated token for a different login), drop the row left under the old account.
    for other in session.exec(
        select(CredentialSource).where(
            CredentialSource.provider_id == provider_id,
            CredentialSource.source_id == source_id,
            CredentialSource.account_id != aid,
        )
    ).all():
        session.delete(other)
    return row


def prune_server_sources(session: Session, provider_id: str, keep_source_ids: set[str]) -> int:
    """Delete a provider's ``server:`` rows whose env var / file is no longer present.

    Server rows have no machine to go stale, so without this a removed env var keeps a
    ghost row (still "valid", still holding its old last success) forever.
    """
    removed = 0
    for row in session.exec(
        select(CredentialSource).where(CredentialSource.provider_id == provider_id)
    ).all():
        if is_server_source_id(row.source_id) and row.source_id not in keep_source_ids:
            session.delete(row)
            removed += 1
    return removed


# File labels the server may still own: Runway's own UI-written GitHub OAuth token.
_RUNWAY_MANAGED_FILE_LABELS = frozenset({"github_oauth.json"})


@dataclass(frozen=True)
class FileSourcePruneResult:
    removed_count: int
    provider_ids: tuple[str, ...]


def prune_unmanaged_server_file_sources(session: Session) -> FileSourcePruneResult:
    """Delete ``server:<provider>:file:<label>`` rows for files the server no longer reads.

    The server used to scan CLI login files in its own host's home (``oauth_creds.json``,
    ``.credentials.json``, ``hosts.yml`` ...) and register each as a source row. It reads
    none of them now (sidecars own those), so without this sweep a stale row would keep
    showing "valid" for users who run no sidecar. Env (``server:*:env:*``), config and
    sidecar rows, and Runway's own ``github_oauth.json``, are kept. Idempotent.
    """
    removed = 0
    providers: set[str] = set()
    for row in session.exec(
        select(CredentialSource).where(col(CredentialSource.source_id).like("server:%"))
    ).all():
        parts = row.source_id.split(":", 3)
        if len(parts) == 4 and parts[2] == "file" and parts[3] not in _RUNWAY_MANAGED_FILE_LABELS:
            session.delete(row)
            removed += 1
            providers.add(row.provider_id)
    from app.services.credential_file_migration import record_file_source_removal

    result = FileSourcePruneResult(removed, tuple(sorted(providers)))
    record_file_source_removal(session, result.removed_count, result.provider_ids)
    return result


HEALTH_DETAILS = {
    "untried": "Not yet tried",
    "auth_failed": "Authentication failed",
    "unavailable": "Collection failed",
    "degraded": "Some requests were rejected; quota was collected",
}


def merge_source_provenance(target: CredentialSource, source: CredentialSource) -> None:
    """Fold *source*'s health and attempt history into *target* when two rows become one.

    Health travels with the attempt that produced it, or ``healthy`` could outlive (or
    predate) the evidence for it: the more recent attempt wins, and rows from before
    attempts were recorded (both NULL) fall back to carrying the legacy health over.
    """
    if source.last_attempt_at and (
        target.last_attempt_at is None or source.last_attempt_at > target.last_attempt_at
    ):
        target.health = source.health
        target.health_detail = source.health_detail
        target.last_attempt_at = source.last_attempt_at
        target.last_success_at = source.last_success_at or target.last_success_at
        target.last_error = source.last_error
        target.consecutive_failures = source.consecutive_failures
        target.next_retry_at = source.next_retry_at
        target.failing_since = source.failing_since
    elif source.last_attempt_at is None and target.last_attempt_at is None:
        target.health = source.health
        target.health_detail = source.health_detail
        target.last_error = source.last_error
        target.last_success_at = source.last_success_at or target.last_success_at


def effective_health(row: CredentialSource) -> str:
    """The health a reader should show for *row*.

    ``health`` defaults to ``"healthy"`` and ``touch_source`` creates rows without any
    collection attempt, so a credential that was only ever registered would read as
    working. Only a recorded attempt can make it healthy; a legacy row that holds a
    non-default health (``auth_failed``...) keeps it, and a row from before attempts were
    recorded that does carry a success is evidence enough.
    """
    if row.health == "healthy" and row.last_attempt_at is None and row.last_success_at is None:
        return "untried"
    return row.health


def configured_account_ids(session: Session, provider_id: str) -> set[str]:
    """Accounts with a ``provider_configs`` row — the only thing a ``config:``
    source row (whose id embeds its account) is written alongside."""
    return set(
        session.exec(
            select(ProviderConfig.account_id).where(ProviderConfig.provider_id == provider_id)
        ).all()
    )


def sidecar_native_accounts(
    session: Session,
    provider_id: str,
    sidecar_id: str | None,
) -> set[str]:
    """The accounts this host's own native collector reported for a provider.

    A credential the sidecar found is filed under the account it resolved
    to, so the row is the machine's own answer to "which account is this
    provider on *this* host". It is the only host-scoped evidence available
    for a provider whose events arrive without an identity — a Hermes event
    is billed to the account of the upstream it called, and the sidecar only
    knows the provider.

    Only the credential the host *presents today* answers. A source is
    current when it is the newest row for its origin, still enabled, and not
    rejected:

    - **Newest per origin.** A rotation — the same file now holding a
      different login — leaves the previous row behind. The newer row is the
      one the host presents; the older one is history.
    - **Enabled.** A source the operator turned off is not an answer.
    - **Not rejected.** A credential the provider refused (``auth_failed``)
      does not identify the account any more.

    The stored expiry is not consulted: it is the *access token's* expiry
    for a rotating login and goes stale between rolls, so the credential
    inventory reads the live bundle instead.

    Two kinds of row are not an answer either way:

    - the ``"default"`` sentinel — the credential's identity was never
      resolved, so nothing is known about the account behind it;
    - the pending slot — a credential whose identity is still being verified
      is filed under a stable placeholder (the source id for Anthropic),
      which is a slot, not an account. A host whose current credential sits
      in that slot has no answer, so it is held back rather than resolved
      against the account it used to have.

    Only rows this sidecar reported are considered: a deployment-wide
    identity must never reach a host that did not report it (the #319 rule).
    """
    rows = session.exec(
        select(CredentialSource).where(
            CredentialSource.provider_id == provider_id,
            CredentialSource.sidecar_id == (sidecar_id or "local"),
        )
    ).all()
    # The newest row per origin is the credential the host presents today.
    newest: dict[str, tuple[Any, list[CredentialSource]]] = {}
    for row in rows:
        seen = row.last_seen
        current = newest.get(row.source_id)
        if current is None or (seen is not None and (current[0] is None or seen > current[0])):
            newest[row.source_id] = (seen, [row])
        elif seen == current[0]:
            current[1].append(row)
    out: set[str] = set()
    for source_id, (_seen, current_rows) in newest.items():
        for row in current_rows:
            account_id = (row.account_id or "").strip()
            if not account_id or account_id == "default":
                continue
            if account_id == pending_cache_slot(provider_id, row.source_id):
                continue  # identity still pending — a slot, not an account
            if not row.enabled:
                continue  # the operator turned this source off
            if row.health == "auth_failed":
                continue  # the provider rejected the credential
            out.add(canonical_account_id(account_id))
    return out


def real_account_ids(session: Session, provider_id: str) -> set[str]:
    """Accounts this provider really has: a configuration, a quota card, usage,
    or the tag/label an operator gave it.

    A ``credential_sources`` row only claims where a credential was filed, and
    an account rename strands that claim; these five kinds of evidence are
    what survive one — the same set ``misidentified_gauge_series`` treats as
    proof an account exists, so a deliberate choice (a credential tagged onto
    a discovered-but-not-yet-collected account) is never read as a stray.
    Source rows on an account outside this set are ``phantom`` — see
    :func:`phantom_accounts`.
    """
    cards = session.exec(
        select(LatestUsage.account_id).where(LatestUsage.provider_id == provider_id).distinct()
    ).all()
    events = session.exec(
        select(UsageEvent.account_id).where(UsageEvent.provider_id == provider_id).distinct()
    ).all()
    tags = session.exec(
        select(CredentialTag.account_id)
        .where(
            or_(
                CredentialTag.target_provider_id == provider_id,
                and_(
                    CredentialTag.provider_id == provider_id,
                    col(CredentialTag.target_provider_id).is_(None),
                ),
            )
        )
        .distinct()
    ).all()
    labels = session.exec(
        select(ProviderAccountLabel.account_id)
        .where(ProviderAccountLabel.provider_id == provider_id)
        .distinct()
    ).all()
    return (
        configured_account_ids(session, provider_id)
        | set(cards)
        | set(events)
        | set(tags)
        | set(labels)
    )


def phantom_accounts(session: Session, provider_id: str) -> set[str]:
    """Accounts a credential source is filed under that have no configuration,
    quota card, usage or operator-set tag/label — the id an account was
    renamed *away from* (a credential-hash id later re-keyed onto its email
    label, typically).

    Never operator intent: an account an operator actually chose keeps at
    least its own configuration, the history it produced, or the identity
    they gave it. Ingest uses this to retire the copy of a source it is
    re-filing (a source belongs to exactly one account), and the Data Health
    ``orphan_credential_sources`` fixer uses it to delete rows nothing else
    will ever move.
    """
    filed = session.exec(
        select(CredentialSource.account_id)
        .where(CredentialSource.provider_id == provider_id)
        .distinct()
    ).all()
    if not filed:
        return set()
    return set(filed) - real_account_ids(session, provider_id)


def record_source_result(row: CredentialSource, health: str) -> None:
    """Record the outcome of one collection attempt on a source row.

    Sets ``health`` and the attempt/success/error provenance. ``healthy`` and
    ``degraded`` both produced quota data, so both count as a success.
    """
    now = datetime.now(UTC)
    row.health = health
    row.health_detail = HEALTH_DETAILS.get(health)
    row.last_attempt_at = now
    if health in ("healthy", "degraded"):
        row.last_success_at = now
        row.last_error = row.health_detail
        row.consecutive_failures = 0
        row.next_retry_at = None
        row.failing_since = None
    else:
        row.last_error = HEALTH_DETAILS.get(health, "Collection failed")
        if not row.consecutive_failures:
            row.failing_since = now
        row.consecutive_failures = (row.consecutive_failures or 0) + 1
        # A rejected credential keeps failing until someone replaces it: back off so it stops
        # costing a call at the head of every cycle. A transient ``unavailable`` is retried as
        # usual (and clears any rest an earlier rejection earned).
        row.next_retry_at = (
            now + retry_backoff(row.consecutive_failures) if health == "auth_failed" else None
        )


def reset_source_retry(session: Session, *, provider_id: str, source_id: str) -> None:
    """Forget a source's failure streak: it holds a different secret now, so retry at once."""
    for row in session.exec(
        select(CredentialSource).where(
            CredentialSource.provider_id == provider_id,
            CredentialSource.source_id == source_id,
        )
    ).all():
        if row.consecutive_failures or row.next_retry_at is not None:
            row.consecutive_failures = 0
            row.next_retry_at = None
            row.failing_since = None
            session.add(row)


def retire_missing_claude_cookie_sources(
    session: Session,
    *,
    sidecar_id: str,
    completed_providers: list[str] | None,
    keep_by_provider: dict[str, set[str]],
) -> list[CredentialSource]:
    """Retire an absent fingerprint only with positive same-store replacement evidence.

    keep_by_provider contains ALL reported origins, never only pending ones. A provider
    can report completion after an inaccessible store returned no cookies, so an empty
    report alone cannot prove logout. Preserve that last-good source until positive
    same-store evidence, rejection, or explicit cleanup makes its state conclusive.
    """
    from app.services.account_identity import split_keyed_origin

    completed = set(completed_providers or []) & {"anthropic", "anthropic_console"}
    reported_stores = {
        provider: {
            base
            for origin in origins
            if origin.startswith(f"cookie:{provider}/")
            for base, fingerprint in [split_keyed_origin(origin)]
            if fingerprint is not None
        }
        for provider, origins in keep_by_provider.items()
    }
    rows = [
        row
        for row in session.exec(
            select(CredentialSource).where(CredentialSource.sidecar_id == sidecar_id)
        ).all()
        if row.provider_id in completed
        and str(row.credential_origin or "").startswith(f"cookie:{row.provider_id}/")
        and row.credential_origin not in keep_by_provider.get(row.provider_id, set())
        and split_keyed_origin(str(row.credential_origin))[0]
        in reported_stores.get(row.provider_id, set())
    ]
    for row in rows:
        session.delete(row)
    return rows
