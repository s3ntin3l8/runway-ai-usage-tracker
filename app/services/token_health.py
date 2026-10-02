"""Token health inspection — expiry parsing, status classification."""

import asyncio
import json
import logging
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, col
from sqlmodel import select as sqlselect

from app.core.config import settings
from app.core.db import engine
from app.core.registry import registry
from app.core.utils import IdentityExtractor, has_refresh_credential, scrub_log
from app.models.db import CredentialSource, ProviderConfig, SidecarRegistry
from app.services import auth_failures
from app.services.account_identity import canonical_account_id
from app.services.credential_provider import CredentialProvider
from app.services.token_cache import _OAUTH_CREDENTIAL_KEYS, TokenCache, token_cache
from app.services.token_refresher import (
    _REFRESH_ENDPOINTS,
    ROTATING_REFRESH_PROVIDERS,
    machine_owns_credential,
)

logger = logging.getLogger(__name__)

EXPIRY_WARNING_SECS = 86400  # 24 hours — for tokens that require manual re-auth
# A sidecar-reported credential nobody has re-reported for this long (and that has no
# live cache bundle) is ``stale``: the machine went away, so its stored expiry / token
# types describe a credential we can no longer vouch for. Derived from the token-cache
# TTL (a live bundle expires after one TTL without a re-push) so a TTL change can't
# desync this and start marking healthy sources stale.
SOURCE_STALE_SECS = 2 * TokenCache.DEFAULT_TTL


def _classify_status(
    exp: float | None,
    is_opaque: bool = False,
    can_refresh: bool = False,
) -> str:
    if exp is None:
        # If we have no JWT expiry, but we have a token value (is_opaque),
        # it's considered "valid" (READY).
        return "valid" if is_opaque else "unknown"
    now = time.time()
    if exp < now:
        return "expired"
    seconds_left = exp - now

    # Tokens with a refresh_token are auto-rolled by TokenAutoRefresher every
    # TOKEN_AUTO_REFRESH_INTERVAL_SECONDS. Short-lived access tokens (Gemini's
    # 60-min JWT) would otherwise sit permanently in the 24h "expiring" bucket.
    # Only warn when the next auto-refresh tick is too late to save the token.
    if can_refresh and settings.TOKEN_AUTO_REFRESH_ENABLED:
        if seconds_left < settings.TOKEN_AUTO_REFRESH_INTERVAL_SECONDS:
            return "expiring"
        return "valid"

    if seconds_left < EXPIRY_WARNING_SECS:
        return "expiring"
    return "valid"


# Origins the dashboard/server manages itself. Their rows are re-seeded every
# collection cycle, so evicting them from the cache would not stick — they are
# changed in Settings → Providers or the server environment instead.
_NON_REMOVABLE_SOURCES = frozenset({"config", "manual_config", "server"})
# Credential fields that share one expiry (the OAuth token family).
_OAUTH_FAMILY_KEYS = _OAUTH_CREDENTIAL_KEYS | {"access_token"}


def _utc(value: datetime | None) -> datetime | None:
    """SQLite hands datetimes back naive; treat them as UTC so they compare with aware ones."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _is_stale_source(last_seen: datetime | None, now: float | None = None) -> bool:
    """True when a durable source hasn't been re-reported within ``SOURCE_STALE_SECS``."""
    seen = _utc(last_seen)
    if seen is None:
        return False
    return (now if now is not None else time.time()) - seen.timestamp() > SOURCE_STALE_SECS


def is_durably_rejected(source: CredentialSource, siblings: Iterable[CredentialSource]) -> bool:
    """Did this source's last collection fail auth *and* does that still matter?

    ``credential_sources.health`` is written per attempted source and only changes when that
    source is tried again, so on its own it over-reports:

    - a **disabled** source is never tried, so its last failure would stand forever;
    - when failover moves past a rejected source to a working sibling, the rejected one keeps
      ``auth_failed`` while the account collects fine. The in-memory flag that used to carry
      this is account-level and clears on any success — "a healthy sibling means collection
      still works" is also how alerts reason — so a rejection only counts here if no enabled
      sibling has succeeded since this source last failed.
    """
    if source.health != "auth_failed" or not source.enabled:
        return False
    attempted = _utc(source.last_attempt_at)
    for other in siblings:
        if other.source_id == source.source_id or not other.enabled:
            continue
        succeeded = _utc(other.last_success_at)
        # No attempt time (a row from before provenance existed): any success supersedes.
        if succeeded is not None and (attempted is None or succeeded >= attempted):
            return False
    return True


# Consecutive failed attempts before an ``unavailable`` source counts as failing, and how old
# its last attempt may be before we stop vouching for the verdict.
FAILING_AFTER_ATTEMPTS = 3
# The streak must also have lasted this long: a provider outage or a rate-limit burst fails
# every source it touches for a few cycles, and none of them is "failing" yet.
FAILING_MIN_DURATION_SECS = 3600
FAILING_MAX_AGE_SECS = 86400


def is_failing(source: CredentialSource, siblings: Iterable[CredentialSource]) -> bool:
    """A credential the provider has not rejected but that keeps failing to collect.

    ``auth_failed`` is the rejection rule (:func:`is_durably_rejected`). This is its
    non-auth twin: the last ``FAILING_AFTER_ATTEMPTS`` attempts all failed over at least
    ``FAILING_MIN_DURATION_SECS`` (a blip, one bad cycle or a short provider outage must not
    flip a status or fire an alert), the verdict is recent, and — as with
    rejection — no enabled sibling has succeeded since, because a working sibling means
    collection still works.
    """
    if source.health != "unavailable" or not source.enabled:
        return False
    if (source.consecutive_failures or 0) < FAILING_AFTER_ATTEMPTS:
        return False
    attempted = _utc(source.last_attempt_at)
    since = _utc(source.failing_since)
    if attempted is None or since is None:
        return False
    if time.time() - attempted.timestamp() > FAILING_MAX_AGE_SECS:
        return False
    if attempted.timestamp() - since.timestamp() < FAILING_MIN_DURATION_SECS:
        return False
    for other in siblings:
        if other.source_id == source.source_id or not other.enabled:
            continue
        succeeded = _utc(other.last_success_at)
        if succeeded is not None and succeeded >= attempted:
            return False
    return True


def apply_failure(status: str, failing: bool) -> str:
    """A credential that looks usable (``valid``/``expiring``/``unknown``) but keeps failing
    to collect reads ``failing``. Rejection (``invalid``) and ``expired`` are already worse."""
    return "failing" if failing and status in ("valid", "expiring", "unknown") else status


def apply_rejection(status: str, rejected: bool) -> str:
    """The single rule for "the provider rejected this credential".

    A rejection upgrades a credential that otherwise looks usable (``valid``,
    ``expiring`` or ``unknown``) to ``invalid``. ``expired`` stays ``expired`` — it is
    already as bad as it gets (alerts detect a rejected-and-expired row separately via
    ``is_flagged``) — and ``stale`` stays ``stale`` (we can't vouch for it either way).
    """
    return "invalid" if rejected and status in ("valid", "expiring", "unknown") else status


def credential_status(
    *,
    exp: float | None,
    token_types: list[str],
    rollable: bool,
    rejected: bool,
    live: bool,
    machine_sourced: bool,
    last_seen: datetime | None,
    now: float | None = None,
    failing: bool = False,
) -> str:
    """One status for one credential: expiry, rejection and staleness combined.

    Composes the same three rules Token Health applies row by row
    (:func:`_classify_status`, :func:`_is_stale_source`, :func:`apply_rejection`), so the
    credential inventory and Token Health cannot drift apart.

    - ``stale``: a machine-sourced credential that isn't live in the cache and hasn't been
      re-reported recently. Its stored expiry is history, so say nothing about it.
    - otherwise the expiry-based classification, upgraded to ``invalid`` on a rejection.
    """
    if machine_sourced and not live and _is_stale_source(last_seen, now):
        return "stale"
    base = _classify_status(
        exp, is_opaque=(exp is None) and bool(token_types), can_refresh=rollable
    )
    return apply_failure(apply_rejection(base, rejected), failing)


def _underlying_account(account_id: str) -> str:
    """Map a synthetic Token Health row id back to the account it stands for."""
    if account_id == "server":
        return "default"
    for prefix in ("config-cookie:", "config:"):
        if account_id.startswith(prefix):
            return account_id[len(prefix) :]
    return account_id


def _is_synthetic(account_id: str) -> bool:
    """Rows Token Health builds itself (server env/file, dashboard config)."""
    return account_id == "server" or account_id.startswith(("config:", "config-cookie:"))


def _build_accounts_by_provider(rows: list[dict[str, Any]]) -> dict[str, set[str]]:
    accounts_by_provider: dict[str, set[str]] = {}
    for r in rows:
        if r.get("assignment_pending") or r.get("identity_pending"):
            continue
        accounts_by_provider.setdefault(r["provider"], set()).add(
            canonical_account_id(_underlying_account(r["account_id"]))
        )
    return accounts_by_provider


def is_flagged(row: dict[str, Any], accounts_by_provider: dict[str, set[str]]) -> bool:
    """True when *row*'s identity currently has a rejected credential flagged
    in `auth_failures` — same matching rules as `_apply_invalid`.

    Matching is by *identity*, not by the borrowing rule: two different
    opaque/fingerprint-keyed accounts of one provider must not flag each
    other. A row matches when its canonical account id equals a flagged id. A
    flagged ``default`` (the unscoped credential — a dashboard-pasted or env
    key, which collectors pin via ``credential_account_id``) matches the
    ``default``/server/config rows, and a non-default row only when that row is
    the provider's sole account (a lone opaque key pushed without identity).
    A ``default`` row is never matched by another identity's rejection, and a
    hash-keyed row is never linked to a flagged email: under-flag rather than
    show a healthy credential as rejected.

    Exposed (not `_`-prefixed) so `app.services.credential_alerts` can reuse
    it directly on already-``expired`` rows, which `_apply_invalid` itself
    skips (see its docstring) — an auth-rejected credential whose access
    token also happens to be expired must still be detectable as rejected.
    """
    flagged = auth_failures.flagged_accounts(row["provider"])
    if not flagged:
        return False
    row_id = canonical_account_id(_underlying_account(row["account_id"]))
    sole_account = accounts_by_provider.get(row["provider"]) == {row_id}
    return row_id in flagged or ("default" in flagged and (row_id == "default" or sole_account))


def _apply_invalid(rows: list[dict[str, Any]]) -> None:
    """Flip healthy-looking rows to ``invalid`` when their credential was rejected.

    Runs after every row exists so statuses are final before redundancy is computed.
    A row is rejected when its own durable source recorded an auth failure at the last
    collection (``_rejected``) or an in-memory rejection flag matches its identity
    (:func:`is_flagged`). See :func:`apply_rejection` for what a rejection can change.
    """
    accounts_by_provider = _build_accounts_by_provider(rows)
    for r in rows:
        rejected = bool(r.pop("_rejected", False))
        failing = bool(r.pop("_failing", False))
        if r.get("assignment_pending") or r.get("identity_pending"):
            continue
        r["status"] = apply_failure(
            apply_rejection(r["status"], rejected or is_flagged(r, accounts_by_provider)), failing
        )


def is_redundancy_sibling(
    healthy_account_id: str, row_account_id: str, *, healthy_is_server_or_config: bool
) -> bool:
    """True when a healthy credential of *healthy_account_id* could stand in for an expired
    one of *row_account_id* (the one rule behind ``redundant`` in Token Health and the
    credential inventory).

    Same canonical account, or an identity-less **cache** entry standing in for an
    identified (email) account — the borrowing direction collectors actually use.
    Two different opaque hashes are different accounts, and server/config credentials
    never count as a sibling of another account.
    """
    h_id = canonical_account_id(_underlying_account(healthy_account_id))
    r_id = canonical_account_id(_underlying_account(row_account_id))
    if h_id == r_id:
        return True
    return "@" in r_id and "@" not in h_id and not healthy_is_server_or_config


def _redundancy_sibling(healthy: dict[str, Any], row: dict[str, Any]) -> bool:
    """True when *healthy* is a credential that could stand in for the expired *row*."""
    return is_redundancy_sibling(
        healthy["account_id"],
        row["account_id"],
        healthy_is_server_or_config=_is_synthetic(healthy["account_id"]),
    )


def _collect_server_credentials() -> dict[str, dict[str, Any]]:
    """Seam for :func:`_scan_server_credentials` (tests isolate the host through it)."""
    return _scan_server_credentials()


def _collect_cli_owned_keys() -> dict[str, set[str]]:
    """Seam: per provider, the credential keys served by a CLI's own login file.

    A rotating provider's login in ``~/.claude`` / ``~/.codex`` is renewed by that CLI, not
    by the server (see ``refresh_policy``). A key an env var also supplies is not listed:
    that value is the server's to manage. Blocking file reads; call via ``asyncio.to_thread``.
    """
    owned: dict[str, set[str]] = {}
    for provider_id in registry.get_all_providers():
        try:
            origins = CredentialProvider.server_credential_origins(provider_id)
        except Exception:
            continue
        env_keys = {k for o in origins if o["source_type"] == "env" for k in o["keys"]}
        keys = {k for o in origins if o.get("cli_owned") for k in o["keys"]} - env_keys
        if keys:
            owned[provider_id] = keys
    return owned


def _scan_server_credentials() -> dict[str, dict[str, Any]]:
    """Credentials the *server itself* discovered (env vars / local files) per provider.

    Blocking (file + DB reads); call through ``asyncio.to_thread``.
    """
    found: dict[str, dict[str, Any]] = {}
    for provider_id in registry.get_all_providers():
        try:
            creds = CredentialProvider.get_credentials(provider_id)
        except Exception as e:
            logger.debug(f"Server credential scan failed for {scrub_log(provider_id)}: {e}")
            continue
        # "config" also covers files inside Runway's own config dir (e.g.
        # github_oauth.json); values that are really a dashboard-saved
        # ProviderConfig key are dropped by the caller's dedup.
        server_tokens = {
            k: v for k, v in creds.items() if v and creds.sources.get(k) in ("server", "config")
        }
        if server_tokens:
            found[provider_id] = server_tokens
    return found


def _row(  # noqa: PLR0913 - one flat record builder; every field is a distinct row column
    provider: str,
    account_id: str,
    *,
    label: str | None,
    source: str | None,
    source_name: str | None,
    token_types: list[str],
    exp: float | None,
    can_refresh: bool,
    ttl_remaining: int = 0,
    rollable: bool = False,
    machine_renewed: bool = False,
) -> dict[str, Any]:
    """Assemble one health record (internal ``_``-prefixed keys are stripped later)."""
    status = _classify_status(
        exp, is_opaque=(exp is None) and bool(token_types), can_refresh=rollable
    )
    return {
        "provider": provider,
        "account_id": account_id,
        "account_label": label,
        "source": source,
        "source_name": source_name,
        "token_types": token_types,
        "status": status,
        "expires_at": (
            datetime.fromtimestamp(exp, tz=UTC).isoformat() if exp is not None else None
        ),
        "ttl_remaining_seconds": ttl_remaining,
        "can_refresh": can_refresh,
        # A rotating provider's login that a machine's CLI renews (the server must not).
        "machine_renewed": machine_renewed,
        "removable": source not in _NON_REMOVABLE_SOURCES,
        # Synthetic rows (config / env) with no expiry are only *assumed* valid.
        "_assumed": source in _NON_REMOVABLE_SOURCES and exp is None,
        "_rollable": rollable,
    }


class TokenHealthService:
    async def get_health(self) -> list[dict[str, Any]]:  # noqa: PLR0915 — composes health sources
        """Return a health record for each known credential."""
        stats = await token_cache.get_all_stats()
        result: list[dict[str, Any]] = []
        seen_token_values: set[str] = set()
        claude_sources = [
            source
            for source in await token_cache._get_source_credentials("anthropic")
            if source["tokens"].get("oauth_token")
            and source["metadata"].get("sidecar_id")
            and source["metadata"].get("identity_pending") is True
        ]
        source_oauth_values = {source["tokens"]["oauth_token"] for source in claude_sources}

        sidecar_names = {}
        durable_sources: list[CredentialSource] = []
        try:
            with Session(engine) as _s:
                for sc in _s.exec(sqlselect(SidecarRegistry)).all():
                    sidecar_names[sc.sidecar_id] = sc.custom_name or sc.hostname or sc.sidecar_id
                durable_sources = list(
                    _s.exec(
                        sqlselect(CredentialSource).where(
                            col(CredentialSource.sidecar_id).is_not(None)
                        )
                    ).all()
                )
                durable_sources = [
                    item
                    for item in durable_sources
                    # Prevent future CredentialSource subclasses with different
                    # field contracts from leaking rows without required strings.
                    if type(item) is CredentialSource
                    and isinstance(item.provider_id, str)
                    and isinstance(item.source_id, str)
                    and isinstance(item.sidecar_id, str)
                ]
        except Exception as e:
            logger.warning(f"Could not load sidecar names for token health: {e}")

        durable_by_account: dict[tuple[str, str], list[CredentialSource]] = {}
        for item in durable_sources:
            durable_by_account.setdefault((item.provider_id, item.account_id), []).append(item)

        candidate_accounts = sorted(
            {
                (provider, account_id)
                for provider, accounts in stats.items()
                for account_id in accounts
            }
            | {(item.provider_id, item.account_id) for item in durable_sources}
        )
        candidate_rows = await asyncio.gather(
            *(
                token_cache.get_source_candidates(provider, account_id)
                for provider, account_id in candidate_accounts
            )
        )
        candidates_by_account = dict(zip(candidate_accounts, candidate_rows, strict=True))

        for provider, accounts in stats.items():
            for acc_id, info in accounts.items():
                tokens = await token_cache.get(provider, acc_id) or {}
                if provider == "anthropic" and tokens.get("oauth_token") in source_oauth_values:
                    tokens = {
                        key: value
                        for key, value in tokens.items()
                        if key not in _OAUTH_FAMILY_KEYS | {"client_id", "expiry_date"}
                    }
                    if not tokens:
                        continue
                logger.debug(f"Token health check for {provider}/{acc_id}: {list(tokens.keys())}")

                # If we have any tokens, track their values to deduplicate later
                for val in tokens.values():
                    if val:
                        seen_token_values.add(f"{provider}:{val}")

                source_val = info.get("source")
                has_refresh_token = has_refresh_credential(tokens)
                source_candidates = candidates_by_account.get((provider, acc_id), [])
                machine_renewed = machine_owns_credential(
                    provider, tokens, source_candidates, merged_source=source_val
                )
                durable_source_ids = {
                    item.source_id
                    for item in durable_sources
                    if item.provider_id == provider and item.account_id == acc_id
                }
                if any(
                    candidate.get("source_id") in durable_source_ids
                    for candidate in source_candidates
                ):
                    # The source-specific durable row below carries this exact
                    # live credential, so suppress the compatibility aggregate.
                    continue
                result.append(
                    _row(
                        provider,
                        acc_id,
                        label=info.get("account_label"),
                        source=source_val,
                        source_name=(
                            sidecar_names.get(source_val, source_val) if source_val else None
                        ),
                        token_types=list(tokens.keys()),
                        exp=IdentityExtractor.exp_from_tokens(tokens),
                        # The manual Refresh button only works where an endpoint exists;
                        # classification still treats any refresh_token as rollable
                        # (a local agent re-pushes e.g. antigravity's short-lived token).
                        can_refresh=has_refresh_token
                        and provider in _REFRESH_ENDPOINTS
                        and not machine_renewed,
                        ttl_remaining=info.get("ttl_remaining", 0),
                        rollable=has_refresh_token,
                        machine_renewed=machine_renewed and has_refresh_token,
                    )
                )

        for source in claude_sources:
            tokens = source["tokens"]
            metadata = source["metadata"]
            pending = bool(metadata.get("identity_pending"))
            sidecar_id = str(metadata["sidecar_id"])
            account_id = source["account_id"]
            identity_hint = metadata.get("identity_hint")
            pending_label = (
                f"Unassigned ({identity_hint})"
                if isinstance(identity_hint, str) and "@" in identity_hint
                else "Unassigned"
            )
            for value in tokens.values():
                if value:
                    seen_token_values.add(f"anthropic:{value}")
            row = _row(
                "anthropic",
                f"unassigned:{source['source_id']}" if pending else account_id,
                label=pending_label if pending else None,
                source=sidecar_id,
                source_name=sidecar_names.get(sidecar_id, sidecar_id),
                token_types=[key for key in tokens if key in _OAUTH_FAMILY_KEYS],
                exp=IdentityExtractor.exp_from_tokens(tokens),
                can_refresh=False,
                ttl_remaining=source["ttl_remaining"],
                rollable=has_refresh_credential(tokens),
                machine_renewed=has_refresh_credential(tokens),
            )
            row.update(
                source_id=source["source_id"],
                assignment_pending=pending,
                removable=False,
            )
            result.append(row)

        for durable_source in durable_sources:
            provider_id = durable_source.provider_id
            account_id = durable_source.account_id
            candidates = candidates_by_account.get((provider_id, account_id), [])
            live = next(
                (item for item in candidates if item.get("source_id") == durable_source.source_id),
                None,
            )
            # Aggregate compatibility rows intentionally have no source_id;
            # this exact-source check only coalesces rows created above.
            if live and any(
                row["provider"] == provider_id and row.get("source_id") == durable_source.source_id
                for row in result
            ):
                # A source-specific row (including pending Claude OAuth) already
                # represents this exact durable source.
                continue
            token_types: list[str] = []
            exp: float | None = None
            durable_rollable = False
            if live:
                live_tokens = live.get("tokens") or {}
                token_types = list(live_tokens)
                exp = IdentityExtractor.exp_from_tokens(live_tokens)
                durable_rollable = has_refresh_credential(live_tokens)
            else:
                try:
                    token_types = json.loads(durable_source.token_types_json or "[]")
                except (TypeError, ValueError):
                    token_types = []
                if isinstance(durable_source.credential_expires_at, datetime):
                    exp = durable_source.credential_expires_at.timestamp()
            row = _row(
                provider_id,
                account_id,
                label="Pending identity" if account_id == "default" else None,
                source=durable_source.source_type,
                source_name=sidecar_names.get(
                    durable_source.sidecar_id or "", durable_source.sidecar_id
                ),
                token_types=token_types,
                exp=exp,
                # A refresh token on a live bundle means the server rolls it before it
                # lapses (and a manual refresh works); without these a 2h-valid Gemini
                # credential read "expiring" — the auto-refresh allowance never applied.
                can_refresh=durable_rollable
                and provider_id in _REFRESH_ENDPOINTS
                and not (provider_id in ROTATING_REFRESH_PROVIDERS and durable_source.sidecar_id),
                rollable=durable_rollable,
                machine_renewed=durable_rollable
                and provider_id in ROTATING_REFRESH_PROVIDERS
                and bool(durable_source.sidecar_id),
            )
            row["_rejected"] = is_durably_rejected(
                durable_source, durable_by_account.get((provider_id, account_id), [])
            )
            row["_failing"] = is_failing(
                durable_source, durable_by_account.get((provider_id, account_id), [])
            )
            if live is None and _is_stale_source(durable_source.last_seen):
                # Not in the live cache and not re-reported recently: the stored
                # token types/expiry are history, not evidence. Without this a
                # removed machine's row reads "valid" forever (or "expired" forever,
                # re-firing alerts); ``stale`` is neither healthy nor alert-worthy.
                row["status"] = "stale"
            row["removable"] = False
            row["source_id"] = durable_source.source_id
            row["sidecar_id"] = durable_source.sidecar_id
            row["identity_pending"] = account_id == "default"
            result.append(row)

        # Also surface API keys / session cookies configured in Settings → Providers.
        # These are stored encrypted in ProviderConfig but never flow through token_cache
        # under their own row, so they would otherwise be invisible to the panel.
        try:
            with Session(engine) as _s:
                configs = _s.exec(
                    sqlselect(ProviderConfig).where(ProviderConfig.enabled == True)  # noqa: E712
                ).all()

            for cfg in configs:
                account = cfg.account_id or "default"
                for value, prefix, token_type in (
                    (cfg.api_key, "config", "api_key"),
                    (cfg.session_cookie, "config-cookie", "session_cookie"),
                ):
                    # Skip if this exact value is already in the live session cache
                    # (and remember it, so the server-file scan below doesn't
                    # re-list a dashboard-saved key as a file credential).
                    if not value:
                        continue
                    seen_key = f"{cfg.provider_id}:{value}"
                    already_listed = seen_key in seen_token_values
                    seen_token_values.add(seen_key)
                    if already_listed:
                        continue
                    result.append(
                        _row(
                            cfg.provider_id,
                            f"{prefix}:{account}",
                            label=cfg.account_label,
                            source="config",
                            source_name="config",
                            token_types=[token_type],
                            exp=None,
                            can_refresh=False,
                        )
                    )
        except Exception as e:
            logger.warning(f"Could not load ProviderConfig credentials for token health: {e}")

        # Credentials the server discovered itself (env vars, local files).
        # They never enter token_cache, so this is the only place they show up.
        try:
            server_creds = await asyncio.to_thread(_collect_server_credentials)
            cli_owned = await asyncio.to_thread(_collect_cli_owned_keys) if server_creds else {}
            for provider, creds in server_creds.items():
                fresh = {
                    k: v for k, v in creds.items() if f"{provider}:{v}" not in seen_token_values
                }
                # One row per credential family: a dead OAuth JWT must not mark an
                # independent API key beside it expired.
                oauth = {k: v for k, v in fresh.items() if k in _OAUTH_FAMILY_KEYS}
                other = {k: v for k, v in fresh.items() if k not in _OAUTH_FAMILY_KEYS}
                for family in (oauth, other):
                    if not family:
                        continue
                    result.append(
                        _row(
                            provider,
                            "server",
                            label=None,
                            source="server",
                            source_name="server",
                            token_types=list(family.keys()),
                            exp=IdentityExtractor.exp_from_tokens(family),
                            can_refresh=False,
                            rollable=has_refresh_credential(family),
                            # The CLI that wrote its login file renews it; the server must
                            # not (and the alert gets the usual grace for an idle CLI).
                            machine_renewed=has_refresh_credential(family)
                            and bool(set(family) & cli_owned.get(provider, set())),
                        )
                    )
        except Exception as e:
            logger.debug(f"Server credential scan for token health failed: {e}")

        _apply_invalid(result)

        # Mark expired, unrefreshable entries as "redundant" when another credential
        # for that account is still healthy. Such an entry can't be auto-rolled and
        # isn't blocking collection — so the dashboard banner should not raise a hard
        # alarm on it alone. See `_redundancy_sibling` for what counts as a sibling;
        # rows that are merely assumed valid (config / env, no expiry) never do.
        healthy = [r for r in result if r["status"] in ("valid", "expiring") and not r["_assumed"]]
        for r in result:
            r["redundant"] = (
                r["status"] == "expired"
                and not r["_rollable"]
                and any(
                    h is not r and h["provider"] == r["provider"] and _redundancy_sibling(h, r)
                    for h in healthy
                )
            )
        for r in result:
            r.pop("_assumed", None)
            r.pop("_rollable", None)

        return result


token_health_service = TokenHealthService()
