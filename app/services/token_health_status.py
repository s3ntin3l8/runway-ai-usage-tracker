"""Pure token-health status rules.

Classifiers and helpers that do not open a ``Session`` and do not touch
``token_cache``. ``app.services.token_health`` re-exports the public names.
"""

import time
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from app.core.config import settings
from app.models.db import CredentialSource
from app.services import auth_failures
from app.services.account_identity import canonical_account_id
from app.services.token_cache import TokenCache

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
