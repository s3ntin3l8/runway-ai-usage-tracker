"""Headless browser smoke test for the Fleet HUD frontend.

Boots the FastAPI app on a free port (with poller + startup-collect stubbed
out and an isolated in-memory DB), seeds LatestUsage + CumulativeUsage,
opens the dashboard in Playwright Chromium, and asserts:

- A Fleet Commander card renders for each (provider, account)
- The status LED row reflects pct_used thresholds (good <70, warn <90, crit >=90)
- The Fuel Dump bar exists with one segment per sidecar
- Clicking the Fuel Dump bar reveals Wingman Pods

A screenshot is written to artifacts/fleet_hud.png for visual review.
"""

import json
import socket
import threading
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import uvicorn
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

playwright = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

ARTIFACTS = Path(__file__).resolve().parents[2] / "artifacts"
ARTIFACTS.mkdir(exist_ok=True)


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server_with_seed():
    """Start uvicorn on a free port with stubbed startup + isolated DB.

    Yields (base_url, engine) — the engine is the in-memory SQLite the app
    reads cards from via the get_session dependency override.
    """
    # Build an in-memory engine the dashboard endpoints will read from
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    # Patch the lifespan so startup doesn't fire real collectors / poller
    from app.services import collector_manager as cm
    from app.services import poller as poller_mod

    cm.manager.collect_all = AsyncMock(return_value=[])
    cm.manager.close = AsyncMock(return_value=None)
    poller_mod.poller.start = lambda: None
    poller_mod.poller.stop = AsyncMock(return_value=None)

    from app.core.db import get_session
    from app.main import app

    def _override_session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = _override_session

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()

    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            with closing(socket.create_connection(("127.0.0.1", port), timeout=0.5)):
                break
        except OSError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        raise RuntimeError("uvicorn did not start in time")

    yield f"http://127.0.0.1:{port}", engine

    server.should_exit = True
    app.dependency_overrides.pop(get_session, None)


def _seed(engine):
    """Insert the test cards + cumulative rows."""
    from app.models.db import CumulativeUsage, LatestUsage

    cards = [
        # Anthropic: weekly @ 35% (good) + monthly @ 92% (crit) → monthly wins as critical_gauge
        (
            "anthropic",
            "acc1",
            "laptop-1",
            "weekly",
            "default",
            {
                "service_name": "Claude Pro Weekly",
                "provider_id": "anthropic",
                "account_id": "acc1",
                "sidecar_id": "laptop-1",
                "icon": "🟠",
                "remaining": "65%",
                "unit": "capacity",
                "health": "good",
                "window_type": "weekly",
                "pct_used": 35.0,
                "used_value": 35.0,
                "limit_value": 100.0,
                "reset_at": "2026-05-12T00:00:00Z",
            },
        ),
        (
            "anthropic",
            "acc1",
            "laptop-1",
            "monthly",
            "default",
            {
                "service_name": "Claude Pro Monthly",
                "provider_id": "anthropic",
                "account_id": "acc1",
                "sidecar_id": "laptop-1",
                "icon": "🟠",
                "remaining": "8%",
                "unit": "capacity",
                "health": "critical",
                "window_type": "monthly",
                "pct_used": 92.0,
                "used_value": 92.0,
                "limit_value": 100.0,
                "reset_at": "2026-06-01T00:00:00Z",
            },
        ),
        # ChatGPT: single window @ 75% (warn)
        (
            "chatgpt",
            "acc1",
            "laptop-1",
            "weekly",
            "default",
            {
                "service_name": "ChatGPT Plus",
                "provider_id": "chatgpt",
                "account_id": "acc1",
                "sidecar_id": "laptop-1",
                "icon": "💬",
                "remaining": "25%",
                "unit": "capacity",
                "health": "warning",
                "window_type": "weekly",
                "pct_used": 75.0,
                "used_value": 75.0,
                "limit_value": 100.0,
                "reset_at": "2026-05-09T00:00:00Z",
            },
        ),
    ]

    month_key = datetime.now(UTC).strftime("%Y-%m")

    with Session(engine) as s:
        for pid, aid, sid, win, var, card in cards:
            s.add(
                LatestUsage(
                    provider_id=pid,
                    account_id=aid,
                    sidecar_id=sid,
                    window_type=win,
                    variant=var,
                    card_json=json.dumps(card),
                )
            )
        # Anthropic/acc1 cumulative for the Fuel Dump bar — one merged row
        # (cross-sidecar merge now happens at the write path; DB stores one row per identity)
        s.add(
            CumulativeUsage(
                provider_id="anthropic",
                account_id="acc1",
                sidecar_id="laptop-1",
                period_type="month",
                period_key=month_key,
                unit_type="tokens_input",
                total_value=16000.0,
            )
        )
        s.commit()


def test_fleet_hud_renders(server_with_seed):
    base_url, engine = server_with_seed
    _seed(engine)

    # Sanity check via raw API before opening the browser
    import urllib.request

    with urllib.request.urlopen(f"{base_url}/api/v1/usage/fleet") as resp:
        fleet_payload = json.loads(resp.read())
    assert len(fleet_payload["fleet"]) == 2, f"fleet endpoint returned {fleet_payload}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        page = ctx.new_page()

        console_errors: list[str] = []
        page.on("pageerror", lambda exc: console_errors.append(f"pageerror: {exc}"))
        page.on(
            "console",
            lambda msg: (
                console_errors.append(f"{msg.type}: {msg.text}") if msg.type == "error" else None
            ),
        )

        page.goto(f"{base_url}/", wait_until="networkidle")
        page.wait_for_selector(".fleet-commander", timeout=10_000)

        commanders = page.locator(".fleet-commander")
        assert commanders.count() == 2, (
            f"expected 2 Fleet Commander cards, got {commanders.count()}; "
            f"console errors: {console_errors}"
        )

        anthropic_card = commanders.filter(has_text="Claude Pro Monthly")
        assert anthropic_card.count() == 1, (
            "anthropic commander should feature the monthly card as critical_gauge"
        )

        led_row = anthropic_card.locator(".led-row")
        assert led_row.count() == 1
        leds = led_row.locator(".led")
        assert leds.count() == 1, f"expected 1 secondary LED for anthropic, got {leds.count()}"

        fuel = anthropic_card.locator(".fuel-dump-bar")
        assert fuel.count() == 1, "Fuel Dump bar missing"
        segments = fuel.locator(".fuel-dump-segment")
        assert segments.count() == 2, f"expected 2 sidecar segments, got {segments.count()}"

        wingman = anthropic_card.locator(".wingman-row")
        assert wingman.count() == 1
        assert wingman.evaluate("el => el.hasAttribute('hidden')") is True, (
            "wingman-row should start hidden"
        )

        fuel.click()
        page.wait_for_timeout(150)
        assert wingman.evaluate("el => el.hasAttribute('hidden')") is False, (
            "wingman-row should reveal after fuel-dump click"
        )
        pods = wingman.locator(".wingman-pod")
        assert pods.count() == 2, f"expected 2 wingman pods, got {pods.count()}"

        page.screenshot(path=str(ARTIFACTS / "fleet_hud.png"), full_page=True)

        if console_errors:
            print("Browser console errors:")
            for e in console_errors:
                print("  ", e)
            assert not console_errors, "browser had console errors"

        browser.close()
