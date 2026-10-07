"""Credential inventory: every discovered credential, who it maps to, and which one
is actually feeding the data.

Built on ``credential_sources`` (sidecar, pasted-config and server env/file sources
all have a row there) and enriched with the live token cache, operator tags, account
labels and the latest quota card. Replaces the need to cross-read Token health, Fleet's
assignment rules / identities and Settings → Providers' source list.

Never returns secret values — only token *types*, origin labels and health.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func
from sqlmodel import Session, col, select

from app.core.db import engine
from app.core.registry import registry
from app.core.utils import IdentityExtractor, has_refresh_credential
from app.models.db import (
    CredentialSource,
    CredentialTag,
    LatestUsage,
    PendingCredentialTag,
    PendingUsageEvent,
    ProviderAccountLabel,
    ProviderConfig,
    SidecarRegistry,
)
from app.models.schemas import (
    CredentialAccountView,
    CredentialInventory,
    CredentialMachineView,
    CredentialProviderView,
    CredentialSourceView,
)
from app.services.account_identity import canonical_account_id, credential_fingerprint
from app.services.credential_provider import CredentialProvider
from app.services.credential_sources import (
    describe_origin_full,
    effective_health,
    is_server_source_id,
    login_hint,
    server_source_id,
)
from app.services.credential_tags import origin_candidates, pick_effective_tag
from app.services.fleet_registry import STALE_THRESHOLD_MINUTES
from app.services.refresh_policy import (
    KEEP_ALIVE_PROVIDERS,
    keep_alive_for,
    parse_provider_flags,
)
from app.services.token_cache import token_cache
from app.services.token_health import (
    credential_status,
    is_durably_rejected,
    is_failing,
    is_flagged,
    is_redundancy_sibling,
)
from app.services.token_refresher import _REFRESH_ENDPOINTS, ROTATING_REFRESH_PROVIDERS

# Best → worst. An account is as healthy as its best enabled source: a working
# credential beside a dead one means collection still works.
_STATUS_RANK = {
    "valid": 0,
    "expiring": 1,
    "unknown": 2,
    "stale": 3,
    "failing": 4,
    "expired": 5,
    "invalid": 6,
}

_TAG_MAPPING = {
    "operator": "operator",
    "identity_claim": "claim",
    "identity_verification": "verified",
    "rotation": "rotation",
}


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _load_token_types(row: CredentialSource) -> list[str]:
    try:
        parsed = json.loads(row.token_types_json or "[]")
    except (TypeError, ValueError):
        return []
    # Legacy/garbage values ("null", "5", an object) must not 500 the whole inventory.
    return [t for t in parsed if isinstance(t, str)] if isinstance(parsed, list) else []


def _resolve_tag(
    tags: dict[tuple[str, str], list[CredentialTag]], row: CredentialSource
) -> CredentialTag | None:
    """Machine-scoped tag first, then the deployment-wide one (same as the read path)."""
    # Exact origin first, then its plain form (a tag written before the origin was
    # fingerprinted). Within one origin the machine-scoped tag beats the deployment-wide
    # one, so a deployment-wide tag on the exact origin still beats a machine-scoped tag
    # on the plain one. Same order as the tag repo and the sidecar.
    for origin in origin_candidates(row.credential_origin or ""):
        tag = pick_effective_tag(tags.get((row.provider_id, origin), []), row.sidecar_id)
        if tag is not None:
            return tag
    return None


def _mapping(
    row: CredentialSource, tag: CredentialTag | None, identity_pending: bool
) -> tuple[str, str | None]:
    if tag is not None:
        return _TAG_MAPPING.get(tag.set_by, "operator"), (
            "machine" if tag.sidecar_id else "all_machines"
        )
    if is_server_source_id(row.source_id):
        return "server", None
    if row.source_type == "config" and row.sidecar_id is None:
        return "config", None
    if identity_pending:
        return "pending", None
    return "local", None


def _data_path(session: Session) -> dict[tuple[str, str], tuple[str | None, str | None]]:
    """``(provider, account) → (data_source, input_source)`` from the freshest quota card.

    ``latest_usage`` has one row per (provider, account, window, variant, model), so the
    freshest row per account is picked in the database — comparing timestamps in Python
    would also mix naive (SQLite-hydrated) and aware datetimes — and only those rows'
    card JSON is parsed.
    """
    newest = (
        select(
            LatestUsage.provider_id,
            LatestUsage.account_id,
            func.max(LatestUsage.updated_at).label("newest"),
        )
        .group_by(col(LatestUsage.provider_id), col(LatestUsage.account_id))
        .subquery()
    )
    rows = session.exec(
        select(LatestUsage).join(
            newest,
            and_(
                col(LatestUsage.provider_id) == newest.c.provider_id,
                col(LatestUsage.account_id) == newest.c.account_id,
                col(LatestUsage.updated_at) == newest.c.newest,
            ),
        )
    ).all()
    out: dict[tuple[str, str], tuple[str | None, str | None]] = {}
    for usage in rows:
        key = (usage.provider_id, usage.account_id)
        if key in out:  # a tie on updated_at: the first row wins
            continue
        try:
            card = json.loads(usage.card_json or "{}")
        except (TypeError, ValueError):
            card = {}
        if not isinstance(card, dict):
            card = {}
        out[key] = (card.get("data_source"), card.get("input_source"))
    return out


def _scan_server_credentials() -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
    """Env/file credentials the server host finds right now, per provider.

    Returns ``(found, scanned)``: ``scanned`` is every provider whose rules were read
    without error, so "absent from ``found``" only means "gone" for those.

    Each origin carries ``shadowed``: ``get_credentials`` (what a collector reads) prefers a
    pasted Settings key, so an env var whose every key is served from elsewhere is unused.
    Blocking file/DB reads; call through ``asyncio.to_thread``.
    """
    found: dict[str, list[dict[str, Any]]] = {}
    scanned: set[str] = set()
    for provider_id in registry.get_all_providers():
        try:
            # Includes Runway's own files (the GitHub device-login token): they are real
            # credentials the server uses, and hiding them left the account with no
            # visible credential and no evidence for data-health checks.
            origins = CredentialProvider.server_credential_origins(provider_id)
            effective = CredentialProvider.get_credentials(provider_id).sources if origins else {}
        except Exception:  # a broken rule must not take the whole inventory down
            continue
        scanned.add(provider_id)
        if origins:
            found[provider_id] = [
                {
                    **o,
                    "shadowed": not o["managed"]
                    and not any(effective.get(k) == "server" for k in o["keys"]),
                }
                for o in origins
            ]
    return found, scanned


def _apply_server_expiry(
    view: CredentialSourceView,
    origin: dict[str, Any],
    now: float,
    *,
    rejected: bool,
    failing: bool = False,
) -> None:
    """Classify a server env/file credential from what the scan saw (expiry, refresh token).

    The scan reads the host's own values, so unlike a machine credential it is always
    "live": a dead OAuth JWT reads expired and a rejected key reads invalid, the same as
    Token Health's ``server`` row.
    """
    # The scan is authoritative even if a stale bundle exists for this row.
    exp = origin.get("exp")
    rollable = bool(origin.get("rollable"))
    view.status = credential_status(
        exp=exp,
        token_types=origin["keys"],
        rollable=rollable,
        rejected=rejected,
        live=True,
        machine_sourced=False,
        last_seen=None,
        failing=failing,
    )
    view.rollable = rollable
    # The scan is authoritative: a stored expiry from an earlier credential must not sit
    # beside a status computed from the one the host has now.
    view.expires_at = datetime.fromtimestamp(exp, tz=UTC).isoformat() if exp is not None else None
    view.expires_in_seconds = int(exp - now) if exp is not None else None
    view.can_refresh = False  # the server's own credential has no source bundle to refresh
    view.rejected = rejected


def _machine_label(view: CredentialSourceView) -> str:
    return view.machine_name or view.machine_id or ""


def _mark_shared(
    accounts: dict[tuple[str, str], list[CredentialSourceView]],
    fingerprints: dict[tuple[str, str], str],
) -> None:
    """Fill ``shared_with``: the same secret reported by more than one machine.

    One login copied between machines is a hazard (the first to renew a rotating refresh token
    signs the others out); one static key everywhere is normal, so the UI only warns inline for
    rollable credentials, and not for peers that have stopped checking in (``shared_with_stale``).
    Machines sharing a home directory legitimately report one origin.
    """
    groups: dict[tuple[str, str], list[CredentialSourceView]] = {}
    for views in accounts.values():
        for view in views:
            fingerprint = fingerprints.get((view.provider_id, view.source_id))
            if fingerprint and view.machine_id:
                groups.setdefault((view.provider_id, fingerprint), []).append(view)
    for group in groups.values():
        if len({v.machine_id for v in group}) < 2:
            continue
        for view in group:
            peers = [other for other in group if other.machine_id != view.machine_id]
            view.shared_with = sorted({_machine_label(p) for p in peers})
            view.shared_with_stale = sorted({_machine_label(p) for p in peers if p.machine_stale})


def _mark_redundant(views: list[CredentialSourceView]) -> None:
    """Flag expired, unrefreshable credentials that another healthy one can stand in for.

    Same rule as Token Health's ``redundant`` (see ``is_redundancy_sibling``): a pasted key or
    env var with no expiry is only *assumed* valid, so it never counts as the healthy sibling.
    """

    def assumed(view: CredentialSourceView) -> bool:
        return view.origin_kind in ("config", "server") and view.expires_at is None

    healthy = [v for v in views if v.status in ("valid", "expiring") and not assumed(v)]
    for view in views:
        view.redundant = (
            view.status == "expired"
            and not view.rollable
            and any(
                h is not view
                and h.provider_id == view.provider_id
                and is_redundancy_sibling(
                    h.account_id,
                    view.account_id,
                    healthy_is_server_or_config=h.origin_kind in ("config", "server"),
                )
                for h in healthy
            )
        )


def _unused_reason(configs: list[ProviderConfig], origin: dict[str, Any]) -> str | None:
    """Why the server would not use ``origin`` — mirrors when the default collector runs
    (``CollectorManager._sync_collectors``) and what ``get_credentials`` prefers."""
    if configs:
        if not any(c.enabled and not c.archived for c in configs):
            return "provider_disabled"
        default = next((c for c in configs if c.account_id == "default"), None)
        if default is None:
            return "account_keyed_config"
        if not default.enabled:
            return "default_disabled"
    return "shadowed_by_config_key" if origin["shadowed"] else None


async def build_inventory() -> CredentialInventory:  # noqa: PLR0915 — one join, kept linear
    now = time.time()
    with Session(engine) as session:
        sources = list(session.exec(select(CredentialSource)).all())
        tags: dict[tuple[str, str], list[CredentialTag]] = {}
        for tag in session.exec(select(CredentialTag)).all():
            tags.setdefault((tag.provider_id, tag.credential_origin), []).append(tag)
        machines = {sc.sidecar_id: sc for sc in session.exec(select(SidecarRegistry)).all()}
        labels: dict[tuple[str, str], str] = {}
        for lab in session.exec(select(ProviderAccountLabel)).all():
            if lab.account_label:
                labels[(lab.provider_id, lab.account_id)] = lab.account_label
        configs_by_provider: dict[str, list[ProviderConfig]] = {}
        for cfg in session.exec(select(ProviderConfig)).all():
            configs_by_provider.setdefault(cfg.provider_id, []).append(cfg)
            if cfg.account_label:
                labels[(cfg.provider_id, cfg.account_id)] = cfg.account_label
        pending_by_sidecar: dict[str, int] = {}
        pending_rows = session.exec(select(PendingCredentialTag)).all()
        for pending in pending_rows:
            pending_by_sidecar[pending.sidecar_id] = (
                pending_by_sidecar.get(pending.sidecar_id, 0) + 1
            )
        rule_count = len(session.exec(select(CredentialTag)).all())
        pending_usage = len(session.exec(select(PendingUsageEvent)).all())
        data_path = _data_path(session)

    stale_cutoff = datetime.now(UTC) - timedelta(minutes=STALE_THRESHOLD_MINUTES)

    def is_stale(sidecar_id: str | None) -> bool:
        """Same rule as Fleet's ``stale``: no check-in within the stale threshold."""
        sc = machines.get(sidecar_id) if sidecar_id else None
        if sc is None or sc.last_seen is None:
            return sc is not None
        return sc.last_seen.replace(tzinfo=UTC) < stale_cutoff

    def machine_name(sidecar_id: str | None) -> str | None:
        if not sidecar_id:
            return None
        sc = machines.get(sidecar_id)
        return (sc.custom_name or sc.hostname or sidecar_id) if sc else sidecar_id

    # A `config:` source row is written only alongside its `provider_configs` row, so
    # one whose config is gone is a claim nothing backs — an account rename that
    # didn't carry its credential rows (the `orphan_credential_sources` fixer deletes
    # these). Hide it rather than render a second, empty identity for the provider,
    # and leave it out of `is_flagged`'s "sole account" accounting too.
    configured_pairs = {
        (cfg.provider_id, cfg.account_id) for cfgs in configs_by_provider.values() for cfg in cfgs
    }

    def is_config_ghost(row: CredentialSource) -> bool:
        return (
            row.source_id.startswith("config:")
            and (row.provider_id, row.account_id) not in configured_pairs
        )

    # Live bundles, per (provider, account), keyed by source id.
    live: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for provider_id, account_id in {(s.provider_id, s.account_id) for s in sources}:
        candidates = await token_cache.get_source_candidates(provider_id, account_id)
        live[(provider_id, account_id)] = {c["source_id"]: c for c in candidates}

    # provider → the identified accounts it has, for ``is_flagged``'s "sole account" rule.
    accounts_by_provider: dict[str, set[str]] = {}
    for row in sources:
        if is_config_ghost(row):
            continue
        if row.sidecar_id is not None and row.account_id in ("default", row.source_id):
            continue  # a machine credential still waiting for an identity
        accounts_by_provider.setdefault(row.provider_id, set()).add(
            canonical_account_id(row.account_id)
        )

    # What the main loop decided per source, so a server row re-classified from the scan
    # below keeps a rejection even when its status ended up ``expired``/``stale``.
    rejected_by_source: dict[tuple[str, str], bool] = {}
    failing_by_source: dict[tuple[str, str], bool] = {}
    siblings_by_account: dict[tuple[str, str], list[CredentialSource]] = {}
    for row in sources:
        if is_config_ghost(row):
            continue
        siblings_by_account.setdefault((row.provider_id, row.account_id), []).append(row)

    accounts: dict[tuple[str, str], list[CredentialSourceView]] = {}
    # (provider, source_id) → fingerprint of the secret that identifies the credential: its
    # refresh token (a login), else its key. Only ever compared; never returned.
    fingerprints: dict[tuple[str, str], str] = {}
    for row in sources:
        if is_config_ghost(row):
            continue
        bundle = live.get((row.provider_id, row.account_id), {}).get(row.source_id)
        tokens = (bundle or {}).get("tokens") or {}
        if bundle is not None:
            token_types = list(tokens)
            exp = IdentityExtractor.exp_from_tokens(tokens)
        else:
            token_types = _load_token_types(row)
            if not token_types and row.source_id.startswith("config:"):
                token_types = ["api_key"]
            exp = row.credential_expires_at.timestamp() if row.credential_expires_at else None
        rollable = has_refresh_credential(tokens)
        machine_sourced = row.sidecar_id is not None
        if machine_sourced and bundle is not None:
            fingerprint = credential_fingerprint(
                tokens.get("refresh_token") or tokens.get("api_key")
            )
            if fingerprint:
                fingerprints[(row.provider_id, row.source_id)] = fingerprint
        # ``rollable`` = something renews it (status stays "valid" between rolls). Who: the
        # server, or, for a rotating provider's machine-owned login, that machine's CLI. The
        # server must not refresh the latter (rotation signs the CLI out), so no Refresh action.
        machine_renewed = (
            machine_sourced and rollable and row.provider_id in ROTATING_REFRESH_PROVIDERS
        )
        server_refreshable = (
            rollable and row.provider_id in _REFRESH_ENDPOINTS and not machine_renewed
        )
        refreshed_by = "machine" if machine_renewed else "server" if server_refreshable else None
        keep_alive: str | None = None
        keep_alive_desired: bool | None = None
        # Independent of rotation and of being live: agy is not a rotating provider, and an
        # expired or withheld login (no live bundle) is exactly when the note matters. A login
        # with no refresh credential (e.g. a pasted access token) has nothing to keep alive.
        if (
            machine_sourced
            and row.provider_id in KEEP_ALIVE_PROVIDERS
            # A live bundle is judged by its values (a blank refresh_token placeholder is not a
            # credential, as for ``rollable``); an offline row only has its stored key names.
            and has_refresh_credential(tokens if bundle is not None else token_types)
        ):
            machine = machines.get(row.sidecar_id or "")
            # Per login: a per-login override / report wins over the sidecar-level ones.
            reported, keep_alive_desired = keep_alive_for(
                row.provider_id,
                reported=getattr(machine, "keep_alive", None),
                desired=getattr(machine, "keep_alive_desired", None),
                reported_providers=parse_provider_flags(
                    getattr(machine, "keep_alive_providers", None)
                ),
                desired_providers=parse_provider_flags(
                    getattr(machine, "keep_alive_desired_providers", None)
                ),
            )
            keep_alive = "unknown" if reported is None else "on" if reported else "off"
        # Only a machine-reported credential can be "waiting for an account": an env var or
        # pasted key on the ``default`` account is that deployment's real account.
        identity_pending = machine_sourced and row.account_id in ("default", row.source_id)
        mapping, scope = _mapping(row, _resolve_tag(tags, row), identity_pending)
        described = describe_origin_full(row.credential_origin)
        origin_type, label = described.kind, described.label
        if not machine_sourced:
            origin_type, label = row.source_type, row.source_label
        # Rejected = this source's last collection failed auth, or an in-memory rejection flag
        # matches its identity under Token Health's rules (a flagged ``default`` matches the
        # default account and a provider's sole account).
        rejected = is_durably_rejected(
            row, siblings_by_account[(row.provider_id, row.account_id)]
        ) or (
            not identity_pending
            and is_flagged(
                {"provider": row.provider_id, "account_id": row.account_id}, accounts_by_provider
            )
        )
        rejected_by_source[(row.provider_id, row.source_id)] = rejected
        failing = is_failing(row, siblings_by_account[(row.provider_id, row.account_id)])
        failing_by_source[(row.provider_id, row.source_id)] = failing
        status = credential_status(
            exp=exp,
            token_types=token_types,
            rollable=rollable,
            rejected=rejected,
            live=bundle is not None,
            machine_sourced=machine_sourced,
            last_seen=row.last_seen,
            failing=failing,
        )
        accounts.setdefault((row.provider_id, row.account_id), []).append(
            CredentialSourceView(
                source_id=row.source_id,
                provider_id=row.provider_id,
                account_id=row.account_id,
                origin_kind=(
                    "machine"
                    if machine_sourced
                    else "server"
                    if is_server_source_id(row.source_id)
                    else "config"
                ),
                origin_type=origin_type,
                label=label,
                origin_app=described.app if machine_sourced else None,
                origin_path=described.path if machine_sourced else None,
                login_hint=login_hint(described.app) if machine_sourced else None,
                machine_id=row.sidecar_id,
                machine_name=machine_name(row.sidecar_id),
                machine_stale=machine_sourced and is_stale(row.sidecar_id),
                mapping=mapping,
                mapping_scope=scope,
                fingerprinted="#" in (row.credential_origin or ""),
                identity_pending=identity_pending,
                status=status,
                expires_at=(
                    datetime.fromtimestamp(exp, tz=UTC).isoformat() if exp is not None else None
                ),
                expires_in_seconds=int(exp - now) if exp is not None else None,
                token_types=token_types,
                can_refresh=server_refreshable and bundle is not None,
                refreshed_by=refreshed_by,
                keep_alive=keep_alive,
                keep_alive_desired=keep_alive_desired,
                rejected=rejected,
                rollable=rollable,
                removable=machine_sourced,
                enabled=row.enabled,
                priority=row.priority,
                live=bundle is not None,
                health=effective_health(row),
                last_seen=_iso(row.last_seen),
                last_attempt_at=_iso(row.last_attempt_at),
                last_success_at=_iso(row.last_success_at),
                last_error=row.last_error,
            )
        )

    _mark_shared(accounts, fingerprints)

    # Server env/file credentials: the read-time scan is authoritative. It adds credentials
    # that are present but never registered (no collection has used them), says why one
    # isn't being used, and hides a registered row whose env var / file has gone away.
    server_found, server_scanned = await asyncio.to_thread(_scan_server_credentials)
    present = {
        (provider_id, server_source_id(provider_id, o["source_type"], o["label"])): o
        for provider_id, origins in server_found.items()
        for o in origins
    }
    seen: set[tuple[str, str]] = set()
    for key, views in list(accounts.items()):
        kept = []
        for view in views:
            if view.origin_kind == "server" and view.provider_id in server_scanned:
                origin = present.get((view.provider_id, view.source_id))
                if origin is None:
                    continue  # the env var / file is gone: a ghost row
                view.unused_reason = _unused_reason(
                    configs_by_provider.get(view.provider_id, []), origin
                )
                _apply_server_expiry(
                    view,
                    origin,
                    now,
                    rejected=rejected_by_source.get((view.provider_id, view.source_id), False),
                    failing=failing_by_source.get((view.provider_id, view.source_id), False),
                )
                seen.add((view.provider_id, view.source_id))
            kept.append(view)
        accounts[key] = kept
    for (provider_id, source_id), origin in present.items():
        if (provider_id, source_id) in seen:
            continue
        view = CredentialSourceView(
            source_id=source_id,
            provider_id=provider_id,
            account_id="default",
            origin_kind="server",
            origin_type=origin["source_type"],
            label=origin["label"],
            mapping="server",
            status="unknown",
            token_types=origin["keys"],
            unused_reason=_unused_reason(configs_by_provider.get(provider_id, []), origin),
        )
        # A rejected unscoped credential is flagged under ``default``; the server's own
        # env/file credential is that account's, exactly as Token Health's ``server`` row.
        _apply_server_expiry(
            view,
            origin,
            now,
            rejected=is_flagged(
                {"provider": provider_id, "account_id": "default"}, accounts_by_provider
            ),
        )
        accounts.setdefault((provider_id, "default"), []).append(view)
    accounts = {key: views for key, views in accounts.items() if views}
    _mark_redundant([view for views in accounts.values() for view in views])

    by_provider: dict[str, list[CredentialAccountView]] = {}
    for (provider_id, account_id), views in accounts.items():
        views.sort(key=lambda v: (v.priority, v.source_id))
        succeeded = [v for v in views if v.last_success_at]
        active = max(succeeded, key=lambda v: v.last_success_at or "") if succeeded else None
        if active is not None:
            active.is_active = True
        # Active first, then healthy ones, then dead ones; newest report first within a tier.
        views.sort(key=lambda v: v.last_seen or "", reverse=True)
        views.sort(key=lambda v: (not v.is_active, _STATUS_RANK.get(v.status, 2)))
        enabled = [v for v in views if v.enabled] or views
        best = min(enabled, key=lambda v: _STATUS_RANK.get(v.status, 2)).status
        data_source, input_source = data_path.get((provider_id, account_id), (None, None))
        by_provider.setdefault(provider_id, []).append(
            CredentialAccountView(
                provider_id=provider_id,
                account_id=account_id,
                account_label=labels.get((provider_id, account_id)),
                status=best,
                identity_pending=all(v.identity_pending for v in views),
                active_source_id=active.source_id if active else None,
                data_source=data_source,
                input_source=input_source,
                sources=views,
            )
        )

    providers = [
        CredentialProviderView(
            provider_id=provider_id,
            name=str(registry.get_provider(provider_id).get("name") or provider_id),
            accounts=sorted(account_views, key=lambda a: (a.identity_pending, a.account_id)),
        )
        for provider_id, account_views in sorted(by_provider.items())
    ]
    counts: dict[str, int] = {}
    for row in sources:
        if row.sidecar_id:
            counts[row.sidecar_id] = counts.get(row.sidecar_id, 0) + 1
    machine_views = [
        CredentialMachineView(
            machine_id=sidecar_id,
            name=machine_name(sidecar_id) or sidecar_id,
            last_seen=_iso(machines[sidecar_id].last_seen) if sidecar_id in machines else None,
            credential_count=counts.get(sidecar_id, 0),
            unmapped_count=pending_by_sidecar.get(sidecar_id, 0),
            stale=is_stale(sidecar_id),
        )
        for sidecar_id in sorted(set(machines) | set(counts) | set(pending_by_sidecar))
    ]
    return CredentialInventory(
        providers=providers,
        machines=machine_views,
        unmapped_count=len(pending_rows),
        rule_count=rule_count,
        pending_usage_events=pending_usage,
    )
