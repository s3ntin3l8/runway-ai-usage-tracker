#!/usr/bin/env python3
"""One-shot cleanup for stale antigravity data.

Background
----------
Two collection eras produced data under different account_ids:

  Old (LSP-era)  — latest_usage + quota_snapshots keyed to the user's Gmail
                   address (e.g. "user@gmail.com"). The LSP collector never
                   created usage_events.

  New (API era)  — usage_events + latest_usage keyed to "default" because the
                   agy token file carries no id_token. The server's new
                   userinfo-based identity resolution will now create cards
                   under the email going forward, but existing "default" rows
                   need to be retagged.

This script:
  Phase A — discover the canonical email from old LSP-era latest_usage cards,
            then delete those cards and their quota_snapshots.
  Phase B — retag "default" rows in latest_usage, quota_snapshots, and
            usage_events to the canonical email so all antigravity data lives
            under a single account_id.
  Phase C — recost all usage_events for antigravity and rebuild
            usage_period_rollup + usage_windows (delegates to recost_events.py).

If there are no old email-keyed cards (clean install), supply --email so Phase
B still knows the target identity.

Run with the server STOPPED (SQLite is single-writer) and APP_HOST=127.0.0.1:

  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
      python scripts/cleanup_antigravity.py --dry-run
  # eyeball the counts, then:
  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
      python scripts/cleanup_antigravity.py --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_SCRIPTS_DIR = str(_REPO_ROOT / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from recost_events import phase_b_recost, phase_c_rollups, phase_d_windows  # noqa: E402
from sqlmodel import Session, delete, select, update  # noqa: E402

from app.core.db import engine  # noqa: E402
from app.models.db import LatestUsage, QuotaSnapshot, UsageEvent  # noqa: E402

_PROVIDER = "antigravity"


def _discover_canonical_email(session: Session) -> str | None:
    """Return the non-default account_id from any existing antigravity latest_usage row."""
    rows = session.exec(
        select(LatestUsage.account_id)
        .where(LatestUsage.provider_id == _PROVIDER)
        .where(LatestUsage.account_id != "default")
        .limit(1)
    ).all()
    return rows[0] if rows else None


def phase_a_old(session: Session, email: str, dry_run: bool) -> tuple[int, int]:
    """Delete LSP-era cards + snapshots keyed to the old email account_id."""
    cards = session.exec(
        select(LatestUsage)
        .where(LatestUsage.provider_id == _PROVIDER)
        .where(LatestUsage.account_id == email)
    ).all()
    snaps = session.exec(
        select(QuotaSnapshot)
        .where(QuotaSnapshot.provider_id == _PROVIDER)
        .where(QuotaSnapshot.account_id == email)
    ).all()
    verb = "Would delete" if dry_run else "Deleting"
    print(
        f"Phase A — {verb} {len(cards)} latest_usage card(s) "
        f"and {len(snaps)} quota_snapshot(s) for account_id={email!r}.",
        flush=True,
    )
    if not dry_run:
        if cards:
            session.exec(
                delete(LatestUsage)
                .where(LatestUsage.provider_id == _PROVIDER)
                .where(LatestUsage.account_id == email)
            )
        if snaps:
            session.exec(
                delete(QuotaSnapshot)
                .where(QuotaSnapshot.provider_id == _PROVIDER)
                .where(QuotaSnapshot.account_id == email)
            )
        session.commit()
    return len(cards), len(snaps)


def phase_b_retag(session: Session, email: str, dry_run: bool) -> dict[str, int]:
    """Retag account_id='default' → email for all three antigravity tables."""
    counts: dict[str, int] = {}

    for label, model, filter_extra in [
        ("latest_usage", LatestUsage, LatestUsage.provider_id == _PROVIDER),
        ("quota_snapshots", QuotaSnapshot, QuotaSnapshot.provider_id == _PROVIDER),
        ("usage_events", UsageEvent, UsageEvent.provider_id == _PROVIDER),
    ]:
        rows = session.exec(
            select(model).where(filter_extra).where(model.account_id == "default")  # type: ignore[attr-defined]
        ).all()
        counts[label] = len(rows)
        verb = "Would retag" if dry_run else "Retagging"
        print(
            f"Phase B — {verb} {len(rows)} {label} row(s): 'default' → {email!r}.",
            flush=True,
        )
        if not dry_run and rows:
            session.exec(
                update(model)  # type: ignore[arg-type]
                .where(filter_extra)
                .where(model.account_id == "default")  # type: ignore[attr-defined]
                .values(account_id=email)
            )
            session.commit()

    return counts


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--dry-run", action="store_true", help="Report what would change without writing."
    )
    g.add_argument("--apply", action="store_true", help="Apply all changes.")
    p.add_argument(
        "--email",
        metavar="EMAIL",
        help="Override the canonical email (auto-discovered from old cards if omitted).",
    )
    p.add_argument(
        "--skip-recost",
        action="store_true",
        help="Skip Phase C (event recost + rollup/window rebuild).",
    )
    args = p.parse_args()
    dry_run = args.dry_run
    prefix = "[DRY-RUN] " if dry_run else ""

    with Session(engine) as session:
        # Discover or accept the canonical email.
        email = args.email or _discover_canonical_email(session)
        if not email:
            print(
                "No old email-keyed cards found and --email not provided.\n"
                "If this is a fresh install with only 'default' data, "
                "supply --email <your@email.com> to run Phase B.",
                file=sys.stderr,
            )
            return 1

        print(f"{prefix}Canonical email: {email}", flush=True)

        n_cards, n_snaps = phase_a_old(session, email, dry_run)
        retag_counts = phase_b_retag(session, email, dry_run)

        if args.skip_recost:
            print("Phase C — skipped (--skip-recost).", flush=True)
            n_updated = n_unchanged = n_ev = n_win = 0
        else:
            print(f"{prefix}Phase C — re-costing events for '{_PROVIDER}'…", flush=True)
            n_updated, n_unchanged, n_zeroed = phase_b_recost(session, [_PROVIDER], None, dry_run)
            print(
                f"{prefix}Phase C-events: {n_updated + n_unchanged + n_zeroed:,} total — "
                f"{n_updated} updated, {n_unchanged} unchanged, {n_zeroed} newly-zeroed.",
                flush=True,
            )
            n_ev = phase_c_rollups(session, [_PROVIDER], dry_run)
            print(f"{prefix}Phase C-rollups: rebuilt from {n_ev:,} event(s).", flush=True)
            n_win = phase_d_windows(session, [_PROVIDER], dry_run)
            print(f"{prefix}Phase C-windows: {n_win:,} window(s) rebuilt.", flush=True)

    print(f"\n{prefix}Summary for '{_PROVIDER}' → {email}:")
    print(f"  Old email-keyed cards deleted    : {n_cards}")
    print(f"  Old email-keyed snapshots deleted: {n_snaps}")
    for table, n in retag_counts.items():
        print(f"  'default' rows retagged ({table}): {n}")
    if not args.skip_recost:
        print(f"  Events recost (updated)          : {n_updated}")
        print(f"  Events recost (unchanged)        : {n_unchanged}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
