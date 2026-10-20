import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.core.date_utils import parse_iso8601_utc
from app.core.utils import HealthCalculator, PaceCalculator, error_card, http_request_with_retry
from app.services.identity_lookup import google_userinfo_email

logger = logging.getLogger(__name__)

# agy uses daily-cloudcode host; the consumer token works on either host,
# but we mirror agy's own preference.
_CLOUDCODE_HOST = "https://daily-cloudcode-pa.googleapis.com"
# Server-sent User-Agent — must look like the agy CLI binary or Google's ESF
# returns 403.
#
# The version in the UA is NOT cosmetic: the backend gates *which quota pools*
# it serves on it. Measured 2026-10-20 against daily-cloudcode-pa.googleapis.com
# with the same token, project and request body, changing only the UA:
#   < 1.2  → "Claude and GPT models" only
#   >= 1.2 → + "Gemini Models"
#   >= 1.3 → + "Gemini 4 Argon" (also unlocked at 1.2 by the aidev_client marker)
# An outdated UA therefore fails silently rather than loudly — the API answers
# 200 with a subset of the pools and Runway reports only the Claude/GPT quota.
# Keep this in step with the installed agy CLI; the follow-up issue tracks
# having the sidecar report `agy --version` so it cannot go stale again.
_USER_AGENT = (
    "antigravity/cli/1.4.3 (aidev_client; os_type=linux; arch=amd64; "
    "cl=1002753436; auth_method=consumer)"
)

# Physical quota pools. The API returns one group per pool and the group's
# display name is the only stable identity it hands us.
POOL_ARGON = "argon"
POOL_GEMINI = "gemini"
POOL_FRONTIER = "frontier"

# Short labels for the quota cards. "argon" stays stable even when Google
# renames the model (Gemini 4 Argon → Gemini 5 Argon), and the pool simply
# disappears when Google folds it back into the Gemini bucket.
POOL_LABELS: dict[str, str] = {
    POOL_ARGON: "Argon",
    POOL_GEMINI: "Gemini",
    POOL_FRONTIER: "Frontier",
}

# The known quota groups, matched by *anchored* patterns. Matching loosely
# (a "gemini" substring test) would fold an unknown pool into a known one —
# and two pools sharing a `variant` share a `latest_usage` row, where the
# second one is silently dropped. The Argon pattern tolerates a version bump
# ("Gemini 4 Argon" → "Gemini 5 Argon") without swallowing any other pool.
_KNOWN_GROUP_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^gemini(?:\s+[\d.]+)?\s+argon$"), POOL_ARGON),
    (re.compile(r"^gemini\s+models?$"), POOL_GEMINI),
    (re.compile(r"^claude\s+and\s+gpt\s+models?$"), POOL_FRONTIER),
)


def _normalize_group_name(group_name: str) -> str:
    """Lower-case a group display name and collapse its whitespace."""
    return re.sub(r"\s+", " ", (group_name or "").strip().lower())


def _pool_slug(normalized_name: str, hash_input: str) -> str:
    """Stable variant key for a quota group we do not know yet.

    Two things keep an unknown group out of a live pool's key:

    * The known pool keys are reserved — a slug that would collide with one
      (e.g. a bare "Argon", which the anchored patterns do not match) is
      prefixed, so the fallback can never take a live pool's key (and never
      slips past the "unknown pool" warning that reports it).
    * A short hash of the group's raw name is appended. The slug folds case
      and punctuation, so "Gemini 5 Code" and "gemini 5-code" would otherwise
      share a pool key — and two pools sharing a key share a `latest_usage`
      row, where the second one is silently dropped. The hash keeps them
      apart while the readable slug stays the card's label.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", normalized_name).strip("-") or "unknown"
    if slug in POOL_LABELS:
        slug = f"unknown-{slug}"
    digest = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()[:6]
    return f"{slug}-{digest}"


def pool_family(group_name: str) -> str:
    """Map a quota group's name to its physical pool family.

    The name is the *resolved* one (the group's display name, else its first
    bucket id — see :func:`group_display_name`), so an unnamed group still
    gets its own key.

    "Gemini Models" → ``gemini``, "Claude and GPT models" → ``frontier``,
    "Gemini 4 Argon" → ``argon`` (a separate paid-plan pool, not part of the
    Gemini bucket).

    A group we do not recognise gets a slug of its own name (plus a short
    hash of the name) instead of being folded into an existing pool: two
    pools sharing a ``variant`` share a ``latest_usage`` slot, and the second
    one silently overwrites the first.
    """
    name = (group_name or "").strip()
    normalized = _normalize_group_name(name)
    for pattern, pool in _KNOWN_GROUP_PATTERNS:
        if pattern.match(normalized):
            return pool
    # Hash the name (whitespace-collapsed, but case- and
    # punctuation-sensitive): only the readable part is folded.
    hash_input = re.sub(r"\s+", " ", name)
    return _pool_slug(normalized, hash_input)


def group_display_name(group: dict[str, Any]) -> str:
    """The quota group's display name, or a fallback built from its buckets.

    A group without a display name would otherwise fall back to "Unknown" —
    and two unnamed groups would slug to the same pool key and overwrite each
    other. The bucket ids are unique, so they make a safe fallback.
    """
    name = str(group.get("displayName") or "").strip()
    if name:
        return name
    for bucket in group.get("buckets", []):
        bucket_id = str(bucket.get("bucketId") or "").strip()
        if bucket_id:
            return bucket_id
    return "unknown"


def pool_label(pool: str, group_name: str = "") -> str:
    """Human label for a pool key.

    Known pools have a fixed label. An unmapped pool keeps the API's own
    spelling of its name ("Claude & GPT models" stays "Claude & GPT models",
    not the title-cased "Claude Gpt Models"). Without a name (a group the API
    sent unnamed) the readable part of the pool key is used.
    """
    if pool in POOL_LABELS:
        return POOL_LABELS[pool]
    if group_name:
        return group_name
    # Unmapped pools carry a trailing hash (their collision-proof id) — the
    # label stays the readable part of it.
    return re.sub(r"-[0-9a-f]{6}$", "", pool).replace("-", " ").title()


# Pools we have already warned about. Process-global on purpose: the poll
# runs every minute, and the operator needs the warning once, not once per
# minute (the first account to poll an unknown pool suppresses it for the
# others — the pool is unknown for everyone anyway).
_WARNED_UNKNOWN_POOLS: set[str] = set()

# Tiers we have already warned about for a missing Gemini pool (same
# once-per-process rationale).
_WARNED_MISSING_GEMINI_TIERS: set[str] = set()


class AntigravityApiMixin:
    """Mixin that fetches Antigravity quota from the Code Assist cloud API.

    Two calls mirror exactly what ``agy /usage`` does:
      1. ``loadCodeAssist`` → cloudaicompanionProject (project id)
      2. ``retrieveUserQuotaSummary`` → QuotaSummary with up to 3 pools
         (gemini weekly+5h, frontier weekly+5h, argon weekly)
    """

    async def _ag_post(
        self,
        client: httpx.AsyncClient,
        method: str,
        body: dict[str, Any],
        state: dict[str, Any],
    ) -> httpx.Response:
        """POST to the Code Assist API with the agy UA + bearer; auto-refresh on 401."""
        headers = {
            "Authorization": f"Bearer {state['token']}",
            "User-Agent": _USER_AGENT,
        }
        resp = await http_request_with_retry(
            client,
            "POST",
            f"{_CLOUDCODE_HOST}/v1internal:{method}",
            json=body,
            headers=headers,
            timeout=15,
        )
        if resp.status_code == 401 and not state["refreshed"]:
            state["refreshed"] = True
            fresh = await self._get_valid_token(client, force_refresh=True)
            if fresh and fresh != state["token"]:
                state["token"] = fresh
                headers["Authorization"] = f"Bearer {fresh}"
                resp = await http_request_with_retry(
                    client,
                    "POST",
                    f"{_CLOUDCODE_HOST}/v1internal:{method}",
                    json=body,
                    headers=headers,
                    timeout=15,
                )
        return resp

    async def _resolve_account_email(self, client: httpx.AsyncClient, token: str) -> str | None:
        """Resolve the Google account email via the userinfo endpoint.

        Called once per collector-instance lifetime when account identity is
        unknown. The agy token file carries no id_token or email claim, so we
        ask Google directly. Result is cached on the instance — no repeated
        calls within the same server run.
        """
        return await google_userinfo_email(client, token)

    async def _collect_via_api(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Fetch Antigravity quota from the Code Assist cloud API.

        Returns up to 5 cards: Gemini-pool weekly, Gemini-pool 5h,
        Frontier-pool (Claude+GPT) weekly, Frontier-pool 5h, Argon-pool weekly.
        """
        now = datetime.now(UTC)
        backoff_until = getattr(self, "_last_429_backoff_until", None)
        if backoff_until and now < backoff_until:
            wait_rem = (backoff_until - now).total_seconds()
            return [
                error_card(
                    "Antigravity",
                    "🛸",
                    f"Rate Limited (429) – backoff for {wait_rem:.0f}s",
                    error_type="rate_limited",
                )
            ]

        token = await self._get_valid_token(client)
        if not token:
            return []

        # Resolve the Google account email once per instance lifetime. The agy
        # token file has no id_token, so we ask the userinfo endpoint. Once
        # known, _tag_results picks it up as account_id/account_label, and
        # _get_active_identities propagates it to the sidecar via the ingest
        # response so future sidecar events land under the same account_id.
        if not self.account_id or self.account_id.lower() in ("default", ""):
            email = await self._resolve_account_email(client, token)
            if email:
                self.account_id = email
                self.account_label = email
                self._account_label_cache = email  # type: ignore[attr-defined]

        # When account_id is already a resolved email (durable seeding) but no
        # label was carried — the agy token has no id_token and the sidecar push
        # stamps only account_id — mirror the email into the label so the
        # dashboard shows the account instead of "Default" (base.py tag fallback).
        if (
            self.account_id
            and "@" in self.account_id
            and (not self.account_label or self.account_label == "Default")
        ):
            self.account_label = self.account_id
            self._account_label_cache = self.account_id  # type: ignore[attr-defined]

        state = {"token": token, "refreshed": False}

        try:
            # 1. Resolve project id (required by retrieveUserQuotaSummary).
            lca_resp = await self._ag_post(
                client,
                "loadCodeAssist",
                {"metadata": {"ideType": "ANTIGRAVITY"}},
                state,
            )
            if lca_resp.status_code == 429:
                wait_sec = float(lca_resp.headers.get("Retry-After") or 300)
                self._last_429_backoff_until = now + timedelta(seconds=wait_sec)
                return [
                    error_card(
                        "Antigravity",
                        "🛸",
                        f"Rate Limited (429) – retry in {wait_sec / 60:.0f}m",
                        error_type="rate_limited",
                    )
                ]
            if lca_resp.status_code != 200:
                logger.warning("Antigravity loadCodeAssist returned %d", lca_resp.status_code)
                if lca_resp.status_code in (401, 403):
                    # agy owns refresh; Runway can't roll the token (no client_id).
                    # Surface a clear call to action instead of silently hiding.
                    return [
                        error_card(
                            "Antigravity",
                            "🛸",
                            "Token expired — run `agy` to refresh",
                            error_type="auth_failed",
                        )
                    ]
                return [
                    error_card(
                        "Antigravity",
                        "🛸",
                        f"API error ({lca_resp.status_code})",
                        error_type="api_error",
                    )
                ]

            lca_data = lca_resp.json()
            project_id = lca_data.get("cloudaicompanionProject", "")

            # Extract tier from loadCodeAssist response
            paid_tier = lca_data.get("paidTier") or {}
            current_tier = lca_data.get("currentTier") or {}
            tier_id_raw = paid_tier.get("id", current_tier.get("id", "unknown"))
            tier_mapping = {
                "g1-pro-tier": "pro",
                "g1-ultra-tier": "ultra",
                "standard-tier": "free",
                "free-tier": "free",
            }
            tier = tier_mapping.get(tier_id_raw, tier_id_raw if tier_id_raw != "unknown" else None)
            if not project_id:
                logger.warning("Antigravity: no cloudaicompanionProject in loadCodeAssist response")
                return []

            # 2. Fetch the full quota summary (both pools × both windows).
            qs_resp = await self._ag_post(
                client,
                "retrieveUserQuotaSummary",
                {"project": project_id},
                state,
            )
            if qs_resp.status_code == 429:
                wait_sec = float(qs_resp.headers.get("Retry-After") or 300)
                self._last_429_backoff_until = now + timedelta(seconds=wait_sec)
                return [
                    error_card(
                        "Antigravity",
                        "🛸",
                        f"Rate Limited (429) – retry in {wait_sec / 60:.0f}m",
                        error_type="rate_limited",
                    )
                ]
            if qs_resp.status_code != 200:
                logger.warning(
                    "Antigravity retrieveUserQuotaSummary returned %d", qs_resp.status_code
                )
                if qs_resp.status_code in (401, 403):
                    return [
                        error_card(
                            "Antigravity",
                            "🛸",
                            "Token expired — run `agy` to refresh",
                            error_type="auth_failed",
                        )
                    ]
                return [
                    error_card(
                        "Antigravity",
                        "🛸",
                        f"API error ({qs_resp.status_code})",
                        error_type="api_error",
                    )
                ]

            self._last_429_backoff_until = None
            qs_data = qs_resp.json()
            groups = qs_data.get("groups", [])
            if not groups:
                return [
                    error_card(
                        "Antigravity", "🛸", "No quota groups returned", error_type="api_error"
                    )
                ]

            results: list[dict[str, Any]] = []
            pools = {pool_family(group_display_name(g)) for g in groups}
            if tier is not None and POOL_GEMINI not in pools:
                # The User-Agent gate is account-independent: a free account
                # loses its Gemini card the same way a paid one does. Its
                # absence means the backend served us a subset of the pools —
                # almost always an outdated User-Agent version (see
                # _USER_AGENT). The check keys on "not None" rather than on a
                # list of paid tier ids, so a tier Google adds later is
                # covered too; a free account is only worth a debug line.
                if tier not in _WARNED_MISSING_GEMINI_TIERS:
                    _WARNED_MISSING_GEMINI_TIERS.add(tier)
                    message = (
                        "Antigravity quota summary returned no Gemini pool for tier %r "
                        "(pools: %s) — the User-Agent version may be too old"
                    )
                    if tier == "free":
                        logger.debug(message, tier, sorted(pools))
                    else:
                        logger.warning(message, tier, sorted(pools))

            for group in groups:
                results.extend(self._group_cards(group, tier, now))

            results.sort(key=lambda x: x.get("window_type", ""), reverse=True)
            return results

        except Exception:
            logger.warning("Antigravity API collection failed", exc_info=True)
            return []

    def _group_cards(
        self,
        group: dict[str, Any],
        tier: str | None,
        now: datetime,
    ) -> list[dict[str, Any]]:
        """Turn one quota group (one physical pool) into a card per window."""
        # The name the API sent (may be empty) — the label of an unmapped pool
        # keeps the API's own spelling. The pool *identity* falls back to the
        # bucket ids when the name is missing.
        raw_name = str(group.get("displayName") or "").strip()
        group_name = group_display_name(group)
        group_description = str(group.get("description") or "").strip()
        pool = pool_family(group_name)
        if pool not in POOL_LABELS and pool not in _WARNED_UNKNOWN_POOLS:
            # A pool we don't know yet: it gets its own variant (never an
            # existing pool's), and a warning so the mapping can be made
            # explicit instead of silently showing up under a made-up name.
            # Logged once per pool per process — the poll runs every minute.
            _WARNED_UNKNOWN_POOLS.add(pool)
            logger.warning(
                "Unknown Antigravity quota group %r — reporting it as pool %r",
                group_name,
                pool,
            )
        cards: list[dict[str, Any]] = []

        for bucket in group.get("buckets", []):
            bucket_id = bucket.get("bucketId", "")
            win_raw = bucket.get("window", "unknown")  # provider raw value: "weekly" or "5h"
            # Map to canonical WindowType.  agy's "5h" is a rolling five-hour
            # window — same semantics as Anthropic's SESSION window.  Any other
            # unknown value falls through to base.py coercion → "unknown".
            win = "session" if win_raw == "5h" else win_raw
            display_name = bucket.get("displayName", win_raw.title())
            rem_frac = bucket.get("remainingFraction", 1.0)
            description = bucket.get("description", "")

            reset_dt: datetime | None = None
            reset_at: str | None = None
            reset_ts = bucket.get("resetTime")
            if reset_ts:
                try:
                    reset_dt = parse_iso8601_utc(reset_ts)
                    reset_at = reset_dt.isoformat()
                except (ValueError, TypeError):
                    pass  # malformed resetTime — leave reset_at/reset_dt unset

            pct_used = round((1.0 - float(rem_frac)) * 100, 4)
            health = HealthCalculator.from_percentage(pct_used)
            pace = PaceCalculator.estimate_longevity(pct_used, reset_dt)

            # Stable pool_id per (pool, canonical-window) so the window
            # cards of one pool never merge in the accumulator.
            pool_id = f"antigravity:{pool}:{win}"

            # The group name identifies the physical pool; the group's own
            # description ("For paid Pro plans, Argon quota is separate.") and
            # the bucket's ("… it will fully refresh in 3 days …") are appended
            # when the API sends them.
            detail = f"{group_name} | {display_name}"
            if group_description:
                detail = f"{detail} — {group_description}"
            if description:
                detail = f"{detail} — {description}"

            card: dict[str, Any] = {
                "service_name": pool_label(pool, raw_name),
                "icon": "🛸",
                "remaining": f"{pct_used:.1f}%",
                "unit": "used",
                "reset_at": reset_at,
                "health": health,
                "pace": pace,
                "detail": detail,
                "used_value": pct_used,
                "limit_value": 100.0,
                "pct_used": pct_used,
                # Aggregate card: pool identity goes in variant so the
                # accumulator window-guard (fires only when model_id != "")
                # is skipped, letting weekly + session coexist in latest_usage.
                # See accumulator.py:226-236 for the aggregate-card convention.
                "model_id": "",
                "variant": pool,
                "unit_type": "percent",
                "window_type": win,
                "data_source": self.DATA_SOURCE_API,
                "input_source": getattr(self, "_current_input_source", "unknown"),
                "quota_pool_id": pool_id,
                "bucket_id": bucket_id,
                "updated_at": now.isoformat(),
            }
            if tier is not None:
                card["tier"] = tier
            cards.append(card)

        return cards
