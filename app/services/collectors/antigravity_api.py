import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.core.date_utils import parse_iso8601_utc
from app.core.utils import HealthCalculator, PaceCalculator, error_card, http_request_with_retry

logger = logging.getLogger(__name__)

# agy uses daily-cloudcode host; the consumer token works on either host,
# but we mirror agy's own preference.
_CLOUDCODE_HOST = "https://daily-cloudcode-pa.googleapis.com"
# Server-sent User-Agent — must look like the agy CLI binary or Google's ESF
# returns 403.  The version string is cosmetic; the UA *prefix* is the gate.
_USER_AGENT = "antigravity/cli/1.0.9 linux/amd64"


class AntigravityApiMixin:
    """Mixin that fetches Antigravity quota from the Code Assist cloud API.

    Two calls mirror exactly what ``agy /usage`` does:
      1. ``loadCodeAssist`` → cloudaicompanionProject (project id)
      2. ``retrieveUserQuotaSummary`` → QuotaSummary with 2 pools × 2 windows
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
        try:
            resp = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5,
            )
            if resp.status_code == 200:
                return resp.json().get("email")
        except Exception:
            logger.debug("Could not resolve Antigravity account email from userinfo", exc_info=True)
        return None

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
                return []

            lca_data = lca_resp.json()
            project_id = lca_data.get("cloudaicompanionProject", "")
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
                return []

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
            for group in groups:
                group_name = group.get("displayName", "Unknown")
                pool_family = "gemini" if "gemini" in group_name.lower() else "frontier"

                for bucket in group.get("buckets", []):
                    bucket_id = bucket.get("bucketId", "")
                    win = bucket.get("window", "unknown")  # "weekly" or "5h"
                    display_name = bucket.get("displayName", win.title())
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
                            pass

                    pct_used = round((1.0 - float(rem_frac)) * 100, 4)
                    health = HealthCalculator.from_percentage(pct_used)
                    pace = PaceCalculator.estimate_longevity(pct_used, reset_dt)

                    # Stable pool_id so weekly and 5h cards don't merge in accumulator.
                    pool_id = f"antigravity:{pool_family}:{win}"

                    results.append(
                        {
                            "service_name": f"{group_name} – {display_name}",
                            "icon": "🛸",
                            "remaining": f"{pct_used:.1f}%",
                            "unit": "used",
                            "reset_at": reset_at,
                            "health": health,
                            "pace": pace,
                            "detail": description or f"{group_name} | {display_name}",
                            "used_value": pct_used,
                            "limit_value": 100.0,
                            "pct_used": pct_used,
                            "model_id": pool_family,
                            "unit_type": "percent",
                            "window_type": win,
                            "data_source": self.DATA_SOURCE_API,
                            "input_source": getattr(self, "_current_input_source", "unknown"),
                            "quota_pool_id": pool_id,
                            "bucket_id": bucket_id,
                            "updated_at": now.isoformat(),
                        }
                    )

            results.sort(key=lambda x: x.get("window_type", ""), reverse=True)
            return results

        except Exception:
            logger.warning("Antigravity API collection failed", exc_info=True)
            return []
