"""The demo's persisted data stays stable when unrelated seed sections grow."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# Import in a subprocess: the seed's scratch-dir guard must run before app.core.db
# creates its process-global engine. Freeze without ticking to compare every field.
DRIVER = r"""
import hashlib
import json
import sys
from collections import defaultdict

import time_machine

with time_machine.travel("2026-10-15T10:00:00Z", tick=False) as clock:
    from scripts.demo import seed_demo as seed
    from sqlalchemy import text
    from sqlmodel import Session

    # Keep unit tests small, while crossing weekly boundaries and exercising
    # the actual seed/ingest/verification paths.
    seed.HISTORY_DAYS = 8
    seed.EVENT_PLAN = [(provider, account, models, (3, 7), cost)
                       for provider, account, models, _, cost in seed.EVENT_PLAN]
    mode = sys.argv[1]
    if mode == "append":
        seed.EVENT_PLAN.append(("github", seed.GITHUB_LOGIN, [("gpt-5.5", 1.0)], (2, 4), None))
    elif mode == "reorder":
        seed.EVENT_PLAN.reverse()
    elif mode == "account":
        provider, _, models, volume, cost = seed.EVENT_PLAN[0]
        seed.EVENT_PLAN.insert(0, (provider, "other@example.com", models, volume, cost))
    elif mode == "history":
        provider, _, models, volume, cost = seed.EVENT_PLAN[0]
        seed.EVENT_PLAN.append((provider, "other@example.com", models, volume, cost))
        original = seed.anthropic_cards
        def extra_cards():
            cards = original()
            extra = dict(cards[1], account_id="other@example.com", account_label="other@example.com")
            return [extra, *reversed(cards)]
        seed.anthropic_cards = extra_cards
    elif mode == "delayed":
        original_history = seed.seed_history
        def delayed_history(session, cards):
            original_history(session, cards)
            clock.shift(60)
        seed.seed_history = delayed_history

    if mode in ("baseline", "repeat", "history", "delayed"):
        seed.main()
    else:
        # Exercise persistence/deduplication too, without rebuilding the unchanged
        # quota tables for each event-plan permutation.
        seed.init_db()
        with Session(seed.engine) as session:
            from app.models.db import ProviderConfig
            for provider, account, *_ in seed.EVENT_PLAN:
                session.add(ProviderConfig(provider_id=provider, account_id=account,
                                           billing_type="subscription"))
            session.commit()
            seed.seed_events(session)

    def digest(rows):
        encoded = sorted(json.dumps(row, sort_keys=True) for row in rows)
        return hashlib.sha256("\n".join(encoded).encode()).hexdigest()

    result = {}
    with seed.engine.connect() as connection:
        for table in ("usage_events", "quota_snapshots", "usage_windows"):
            groups = defaultdict(list)
            for row in connection.execute(text("SELECT * FROM " + table)).mappings():
                payload = dict(row)
                payload.pop("id")  # Surrogate insertion order isn't rendered.
                if table == "usage_events":
                    key = (payload["provider_id"], payload["account_id"])
                elif table == "quota_snapshots":
                    key = (payload["provider_id"], payload["account_id"],
                           payload["window_type"], payload["variant"], payload["model_id"])
                else:
                    key = (payload["provider_id"], payload["account_id"],
                           payload["window_type"], payload["series_variant"],
                           payload["series_model_id"])
                groups[json.dumps(key)].append(payload)
            result[table] = {key: {"count": len(rows), "sha256": digest(rows)}
                             for key, rows in groups.items()}
            if table == "quota_snapshots":
                for key, rows in groups.items():
                    result[table][key]["latest_ts"] = max(row["ts"] for row in rows)
    print("SEED_RESULT=" + json.dumps(result))
"""


def _seed(tmp_path: Path, mode: str) -> dict[str, dict[str, dict[str, int | str]]]:
    scratch = tmp_path / f"runway-demo-{mode}"
    done = subprocess.run(
        [sys.executable, "-c", DRIVER, mode],
        cwd=REPO,
        env={**os.environ, "RUNWAY_CONFIG_DIR": str(scratch), "PYTHONHASHSEED": "random"},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    result = next(
        line.removeprefix("SEED_RESULT=")
        for line in done.stdout.splitlines()
        if line.startswith("SEED_RESULT=")
    )
    return json.loads(result)


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("seed-regressions")
    return root, _seed(root, "baseline")


def test_fixed_clock_reproduces_seeded_database_rows(seeded) -> None:
    root, baseline = seeded
    assert all(baseline.values())
    assert _seed(root, "repeat") == baseline


def test_current_quota_snapshot_uses_seed_clock_after_slow_history(seeded) -> None:
    root, baseline = seeded
    snapshots = _seed(root, "delayed")["quota_snapshots"]
    assert snapshots == baseline["quota_snapshots"]
    assert {series["latest_ts"] for series in snapshots.values()} == {"2026-10-15 10:00:00.000000"}


@pytest.mark.parametrize("mode", ["append", "reorder", "account"])
def test_event_plan_changes_preserve_existing_accounts(seeded, mode: str) -> None:
    root, baseline = seeded
    original = baseline["usage_events"]
    changed = _seed(root, mode)["usage_events"]
    assert original
    assert {key: changed[key] for key in original} == original
    if mode != "reorder":
        assert len(changed) == len(original) + 1
    else:
        assert changed == original


def test_extra_history_card_preserves_other_streams(seeded) -> None:
    root, baseline = seeded
    changed = _seed(root, "history")
    original_history = baseline["quota_snapshots"]
    assert len(changed["quota_snapshots"]) == len(original_history) + 1
    assert {key: changed["quota_snapshots"][key] for key in original_history} == original_history
    original_events = baseline["usage_events"]
    assert len(changed["usage_events"]) == len(original_events) + 1
    assert {key: changed["usage_events"][key] for key in original_events} == original_events
    original_windows = baseline["usage_windows"]
    assert len(changed["usage_windows"]) == len(original_windows) + 1
    assert {key: changed["usage_windows"][key] for key in original_windows} == original_windows
