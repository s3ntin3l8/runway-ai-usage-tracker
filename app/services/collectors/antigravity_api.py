import logging
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
# display name is the only stable identity it hands us. The order matters:
# "Gemini 4 Argon" is a *separate* paid-plan pool that also says "gemini", so
# the Argon check must win — otherwise both pools land in the same
# `antigravity:gemini:<window>` slot and overwrite each other in LatestUsage.
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


def pool_family(group_name: str) -> str:
    """Map a quota group's display name to its physical pool family.

    "Gemini Models" → ``gemini``, "Claude and GPT models" → ``frontier``,
    "Gemini 4 Argon" → ``argon`` (a separate paid-plan pool, not part of the
    Gemini bucket).
    """
    lowered = (group_name or "").lower()
    if "argon" in lowered:
        return POOL_ARGON
    if "gemini" in lowered:
        return POOL_GEMINI
    return POOL_FRONTIER


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

        Returns up to 4 cards: Gemini-pool weekly, Gemini-pool 5h,
        Frontier-pool (Claude+GPT) weekly, Frontier-pool 5h.
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
            pools = {pool_family(g.get("displayName", "")) for g in groups}
            if tier in ("pro", "ultra") and POOL_GEMINI not in pools:
                # A paid tier always has a Gemini pool. Its absence means the
                # backend served us a subset of the pools — almost always an
                # outdated User-Agent version (see _USER_AGENT).
                logger.warning(
                    "Antigravity quota summary returned no Gemini pool for tier %r "
                    "(pools: %s) — the User-Agent version may be too old",
                    tier,
                    sorted(pools),
                )

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
        group_name = group.get("displayName", "Unknown")
        pool = pool_family(group_name)
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

            # The group name identifies the physical pool; the bucket's
            # own description ("… it will fully refresh in 3 days …") is
            # appended when the API sends one.
            detail = f"{group_name} | {display_name}"
            if description:
                detail = f"{detail} — {description}"

            card: dict[str, Any] = {
                "service_name": POOL_LABELS.get(pool, pool.capitalize()),
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
