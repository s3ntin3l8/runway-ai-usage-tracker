"""Synthetic demo data for a scratch Runway database.

Run by scripts/demo/instance.py with RUNWAY_CONFIG_DIR pointing at a scratch
directory; it refuses any other database. Everything is generated relative to
now from a fixed random seed, so a capture always looks current and repeated
runs produce the same shapes.

The data goes through Runway's own code paths wherever they exist:
- Limit cards come from each collector's own parse function fed a synthetic
  provider response, tagged with `_tag_results`, and written with
  `upsert_latest_usage` as a sidecar would (server-collected cards would be
  marked stale by the poller after an hour).
- Usage events go through `EventIngestor`, which prices them and maintains the
  period rollups; past quota windows are closed with `close_window`.
- Only the backdated quota history is inserted directly (`QuotaSnapshot` via
  its SQLModel class), because the write path stamps it with the current time.
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# Refuse to run against anything but a scratch config dir. Checked before any
# `app` import, because importing app.core.db opens the database.
_config_dir = os.environ.get("RUNWAY_CONFIG_DIR", "")
if not _config_dir or "runway-demo-" not in _config_dir or not Path(_config_dir).is_absolute():
    sys.exit("seed_demo: RUNWAY_CONFIG_DIR must be the demo's scratch dir (run via make demo)")

from sqlmodel import Session  # noqa: E402

from app.core.db import engine, init_db  # noqa: E402
from app.models.db import ProviderConfig, QuotaSnapshot  # noqa: E402
from app.models.schemas import UsageEventPush  # noqa: E402
from app.services.accumulator import upsert_latest_usage  # noqa: E402
from app.services.collectors._anthropic_common import ANTHROPIC_WINDOW_NAME_MAP  # noqa: E402
from app.services.collectors.anthropic import AnthropicCollector  # noqa: E402
from app.services.collectors.chatgpt import ChatGPTCollector  # noqa: E402
from app.services.collectors.gemini import GeminiCollector  # noqa: E402
from app.services.collectors.github import GitHubCollector  # noqa: E402
from app.services.collectors.ollama import OllamaCollector  # noqa: E402
from app.services.collectors.opencode import OpenCodeCollector  # noqa: E402
from app.services.data_health.jobs import _scan_sync  # noqa: E402
from app.services.event_ingestor import EventIngestor  # noqa: E402
from app.services.fleet_registry import fleet_registry  # noqa: E402
from app.services.window_closer import close_window  # noqa: E402

# Fictional identities only: these render in public screenshots.
EMAIL = "dev@example.com"
GITHUB_LOGIN = "demo-dev"
SIDECARS = [("demo-mbp", "Darwin/24.6.0"), ("demo-workstation", "Linux/6.8.0")]
SIDECAR = SIDECARS[0][0]
PROJECTS = ["aurora-web", "orbit-api", "beacon-cli"]
HISTORY_DAYS = 60

NOW = datetime.now(UTC).replace(second=0, microsecond=0)
rng = random.Random(42)

WINDOW = {
    "session": timedelta(hours=5),
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
    "monthly": timedelta(days=30),
}
# Snapshot spacing per window type: dense enough for the forecast's buckets.
STEP = {
    "session": timedelta(minutes=15),
    "daily": timedelta(hours=1),
    "weekly": timedelta(hours=1),
    "monthly": timedelta(hours=6),
}


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


# --- Limit cards, via each collector's own parser -----------------------------


def anthropic_cards() -> list[dict[str, Any]]:
    c = AnthropicCollector(account_id=EMAIL, account_label=EMAIL)
    data = {
        "limits": [
            {
                "kind": "session",
                "group": "session",
                "percent": 48,
                "severity": "normal",
                "resets_at": iso(NOW + timedelta(hours=1, minutes=25)),
                "scope": None,
            },
            {
                "kind": "weekly_all",
                "group": "weekly",
                "percent": 61,
                "severity": "normal",
                "resets_at": iso(NOW + timedelta(days=2, hours=6)),
                "scope": None,
            },
            {
                "kind": "weekly_scoped",
                "group": "weekly",
                "percent": 37,
                "severity": "normal",
                "resets_at": iso(NOW + timedelta(days=2, hours=6)),
                "scope": {"model": {"id": "sonnet", "display_name": "Sonnet"}, "surface": None},
            },
        ]
    }
    parsed = c._parse_oauth_response(
        data, ANTHROPIC_WINDOW_NAME_MAP, {"tier": "Max 5x", "email": EMAIL}
    )
    return c._tag_results(parsed)


def chatgpt_cards() -> list[dict[str, Any]]:
    c = ChatGPTCollector(account_id=EMAIL, account_label=EMAIL)
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "plan_type": "pro",
        "email": EMAIL,
        "rate_limit": {
            "primary_window": {
                "used_percent": 22,
                "reset_at": int((NOW + timedelta(hours=3, minutes=10)).timestamp()),
                "limit_window_seconds": 18000,
            },
            "secondary_window": {
                "used_percent": 96,
                "reset_at": int((NOW + timedelta(days=1, hours=4)).timestamp()),
                "limit_window_seconds": 604800,
            },
        },
    }
    client = MagicMock()
    client.request = AsyncMock(return_value=resp)
    return c._tag_results(asyncio.run(c._fetch_api_data(client, "demo", None, "api")))


def gemini_cards() -> list[dict[str, Any]]:
    c = GeminiCollector(account_id=EMAIL, account_label=EMAIL)
    tier = MagicMock(status_code=200)
    tier.json.return_value = {"paidTier": {"id": "g1-pro-tier"}, "cloudaicompanionProject": "p"}
    quota = MagicMock(status_code=200)
    reset = iso(NOW + timedelta(hours=14))
    quota.json.return_value = {
        "buckets": [
            {"modelId": "gemini-2.5-pro", "remainingFraction": 0.71, "resetTime": reset},
            {"modelId": "gemini-2.5-flash", "remainingFraction": 0.93, "resetTime": reset},
        ]
    }
    with (
        patch.object(c, "_get_valid_token", AsyncMock(return_value="demo")),
        patch(
            "app.services.collectors.gemini_api.http_request_with_retry",
            AsyncMock(side_effect=[tier, quota]),
        ),
    ):
        return c._tag_results(asyncio.run(c._collect_via_api(None)))


def github_cards() -> list[dict[str, Any]]:
    c = GitHubCollector(account_id=GITHUB_LOGIN, account_label=GITHUB_LOGIN)
    c._identity = None
    data = {
        "copilot_plan": "individual",
        "quota_reset_date_utc": iso(NOW + timedelta(days=12)),
        "quota_snapshots": {
            "premium_interactions": {"entitlement": 300, "remaining": 118, "unlimited": False},
        },
    }
    return c._tag_results(c._parse_api_responses(SimpleNamespace(status_code=200), data))


def opencode_cards() -> list[dict[str, Any]]:
    c = OpenCodeCollector(account_id=EMAIL, account_label=EMAIL)
    usd = 100_000_000  # microcents per dollar
    body = {
        "subscriberUserId": EMAIL,
        "access": {
            "endsAt": iso(NOW + timedelta(days=21)),
            "meters": {
                "fiveHour": {
                    "usedMicroCents": int(4.1 * usd),
                    "resetsAt": iso(NOW + timedelta(hours=2)),
                },
                "week": {
                    "usedMicroCents": int(18.6 * usd),
                    "resetsAt": iso(NOW + timedelta(days=4)),
                },
                "month": {
                    "usedMicroCents": int(41.0 * usd),
                    "resetsAt": iso(NOW + timedelta(days=21)),
                },
            },
        },
    }
    return c._tag_results(c._build_cards_from_go_status(body, "sidecar"))


def ollama_cards() -> list[dict[str, Any]]:
    # No account_id: the parser then takes the identity from the page itself.
    c = OllamaCollector()
    html = f"""
    <html><head><span id="header-email">{EMAIL}</span></head><body>
      <div>Cloud Usage <span class="capitalize">Pro</span></div>
      <div class="usage-block"><span>Session usage</span>
        <div class="bar" style="width: 18.0%"></div><span class="text">18.0% used</span>
        <span data-time="{iso(NOW + timedelta(hours=3))}">Resets soon</span></div>
      <div class="usage-block"><span>Weekly usage</span>
        <div class="bar" style="width: 9.0%"></div><span class="text">9.0% used</span>
        <span data-time="{iso(NOW + timedelta(days=5))}">Resets later</span></div>
    </body></html>"""
    return c._tag_results(c._parse_html(html))


# --- Quota history --------------------------------------------------------------


def _card_pct(card: dict[str, Any]) -> float | None:
    if card.get("pct_used") is not None:
        return float(card["pct_used"])
    used, limit = card.get("used_value"), card.get("limit_value")
    if used is not None and limit:
        return 100.0 * float(used) / float(limit)
    return None


def history(card: dict[str, Any]) -> Iterator[tuple[datetime, float, datetime]]:
    """(ts, pct_used, reset_at) every STEP for HISTORY_DAYS, ending on the card's value."""
    window_type = card.get("window_type") or "weekly"
    duration, step = WINDOW.get(window_type), STEP.get(window_type)
    pct_now, reset_raw = _card_pct(card), card.get("reset_at")
    if duration is None or step is None or pct_now is None or not reset_raw:
        return
    reset = datetime.fromisoformat(str(reset_raw).replace("Z", "+00:00"))
    # Offset so no sample lands exactly on a window boundary, where it would be
    # read as the first point of the next window.
    start = NOW - timedelta(days=HISTORY_DAYS) + timedelta(minutes=7)
    finals: dict[int, float] = {}
    t = start
    while t < NOW - timedelta(minutes=5):
        k = int((reset - t) / duration)  # 0 = the current window
        window_end = reset - k * duration
        x = 1 - (window_end - t) / duration  # 0..1 through this window
        if k == 0:
            x_now = max(1 - (reset - NOW) / duration, 0.05)
            pct = pct_now * min(x / x_now, 1.0) ** 0.9
        else:
            final = finals.setdefault(k, rng.uniform(30, 98))
            pct = final * x**0.9
        yield t, round(max(0.0, min(pct, 100.0)), 1), window_end
        t += step


def seed_history(session: Session, cards: list[dict[str, Any]]) -> None:
    for card in cards:
        variant = card.get("variant") or ""
        rows = [
            QuotaSnapshot(
                provider_id=card["provider_id"],
                account_id=card["account_id"],
                window_type=card.get("window_type") or "weekly",
                variant="" if variant == "default" else variant,
                model_id=card.get("model_id") or "",
                ts=ts,
                pct_used=pct,
                reset_at=reset,
            )
            for ts, pct, reset in history(card)
        ]
        session.add_all(rows)
    session.commit()


# --- Usage events ---------------------------------------------------------------

# (provider, account, models with weights, daily message range, cost per message
# when the provider has no price table, or None to let Runway price it)
EVENT_PLAN: list[tuple[str, str, list[tuple[str, float]], tuple[int, int], float | None]] = [
    (
        "anthropic",
        EMAIL,
        [("opus-4.8", 0.45), ("sonnet-4.6", 0.4), ("haiku-4.5", 0.15)],
        (40, 140),
        None,
    ),
    ("chatgpt", EMAIL, [("gpt-5.5", 0.6), ("gpt-5.3-codex", 0.4)], (15, 70), None),
    ("gemini", EMAIL, [("pro", 0.7), ("flash", 0.3)], (5, 30), None),
    ("opencode", EMAIL, [("kimi-k2", 1.0)], (5, 25), 0.012),
]


def _pick(models: list[tuple[str, float]]) -> str:
    return rng.choices([m for m, _ in models], weights=[w for _, w in models])[0]


def events() -> Iterator[UsageEventPush]:
    n = 0
    for provider, account, models, (lo, hi), unit_cost in EVENT_PLAN:
        for day in range(HISTORY_DAYS, -1, -1):
            date = NOW - timedelta(days=day)
            # Quieter weekends, a little growth over the period.
            weekend = date.weekday() >= 5
            volume = int(
                rng.randint(lo, hi)
                * (0.35 if weekend else 1.0)
                * (0.7 + 0.3 * (1 - day / HISTORY_DAYS))
            )
            # A few working sessions a day, each 20 minutes to 2 hours long.
            midnight = date.replace(hour=0, minute=0)
            sessions = [
                (
                    f"{provider[:2]}{day:02d}{i:02d}{rng.randrange(16**6):06x}",
                    midnight + timedelta(minutes=rng.randint(8 * 60, 21 * 60)),
                    rng.randint(20, 120),
                )
                for i in range(rng.randint(2, 6))
            ]
            for _ in range(volume):
                session_id, start, length = rng.choice(sessions)
                ts = start + timedelta(minutes=rng.randint(0, length))
                if ts >= NOW:
                    continue
                n += 1
                output = rng.randint(300, 4_000)
                yield UsageEventPush(
                    provider_id=provider,
                    account_id=account,
                    account_source="local",
                    event_id=f"demo-{provider}-{n}",
                    ts=iso(ts),
                    model_id=_pick(models),
                    session_id=session_id,
                    cwd=f"/home/dev/projects/{rng.choice(PROJECTS)}",
                    git_branch="main",
                    tokens_input=rng.randint(800, 12_000),
                    tokens_output=output,
                    tokens_cache_read=rng.randint(20_000, 180_000),
                    tokens_cache_create=rng.randint(500, 9_000),
                    tool_calls=rng.randint(0, 6),
                    cost_usd=unit_cost * (0.5 + output / 4_000) if unit_cost else None,
                )


def seed_events(session: Session) -> None:
    batch: list[UsageEventPush] = []
    for push in events():
        batch.append(push)
        if len(batch) == 500:
            EventIngestor(session).ingest(batch, sidecar_id=SIDECAR)
            batch = []
    if batch:
        EventIngestor(session).ingest(batch, sidecar_id=SIDECAR)


def close_past_windows(session: Session, cards: list[dict[str, Any]]) -> None:
    """Closed weekly windows for the History page's quota-window table."""
    for card in cards:
        if card.get("window_type") != "weekly" or card.get("model_id") or not card.get("reset_at"):
            continue
        reset = datetime.fromisoformat(str(card["reset_at"]).replace("Z", "+00:00"))
        for k in range(1, HISTORY_DAYS // 7 + 1):
            end = reset - k * WINDOW["weekly"]
            close_window(
                session,
                provider_id=card["provider_id"],
                account_id=card["account_id"],
                window_type="weekly",
                window_start=end - WINDOW["weekly"],
                window_end=end,
                series_variant="default",
                limit_value=100.0,
                pct_used=round(rng.uniform(35, 97), 1),
            )
    session.commit()


def canonical(card: dict[str, Any]) -> dict[str, Any]:
    """Key a card to its real identity, as upsert_latest_usage would.

    Collectors that learn the identity from the response (Ollama) tag cards
    "default" with the email as label; history written under "default" would
    be flagged by Runway's own data-health checks.
    """
    if card.get("account_id") in (None, "", "default") and card.get("account_label"):
        card["account_id"] = str(card["account_label"]).lower()
    return card


def main() -> None:
    init_db()
    builders = [
        anthropic_cards,
        chatgpt_cards,
        gemini_cards,
        github_cards,
        opencode_cards,
        ollama_cards,
    ]
    cards = [canonical(card) for build in builders for card in build()]
    with Session(engine) as s:
        # Every seeded account is configured, as on a real install; Runway's
        # data-health checks flag gauge data with no config or usage behind it.
        for provider_id, account_id in sorted({(c["provider_id"], c["account_id"]) for c in cards}):
            s.add(
                ProviderConfig(
                    provider_id=provider_id,
                    account_id=account_id,
                    account_label=account_id,
                    billing_type="subscription",
                )
            )
        s.commit()
        for sidecar_id, platform in SIDECARS:
            fleet_registry.upsert_sidecar(
                sidecar_id,
                "127.0.0.1",
                s,
                sidecar_version="3.0.0",
                os_platform=platform,
                keep_alive=True,
            )
        seed_history(s, cards)
        seed_events(s)
        for card in cards:
            upsert_latest_usage(s, card, sidecar_id_override=SIDECAR)
        s.commit()
        close_past_windows(s, cards)
    verify()
    print(f"seed_demo: {len(cards)} cards, {HISTORY_DAYS} days of history and events")


# The one finding a demo legitimately has: no alert webhook (that needs a real
# Discord/Slack URL).
EXPECTED_FINDINGS = {"alert_channels"}


def verify() -> None:
    """Fail the seed if Runway's own data-health scan finds inconsistencies in it."""
    problems = {
        check_id: report.total_count
        for check_id, report in _scan_sync().items()
        if report.total_count and check_id not in EXPECTED_FINDINGS
    }
    if problems:
        sys.exit(f"seed_demo: data-health findings in the demo data: {problems}")


if __name__ == "__main__":
    main()
