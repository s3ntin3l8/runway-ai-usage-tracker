"""One-off repair for the ChatGPT May 17-23 oscillation.

Between roughly 2026-05-17 and 2026-05-19 the ChatGPT /backend-api/wham/usage
endpoint intermittently returned ~4% used while the real state was ~100%, so
`record_quota_snapshot` persisted a string of false dips. The final transition
to low pct_used was the genuine weekly reset; everything before it that dropped
below the threshold is bogus and must be rewritten to the surrounding baseline.

The script also removes any `usage_windows` rows that `window_closer` archived
during the oscillation — those can only exist if a spurious reset_at advance
triggered `_maybe_close_previous_window`.

Subcommands:
    inspect   List ChatGPT snapshots + closed windows in the range; recommend a cutoff.
    plan      Show exact UPDATEs / DELETEs given an explicit --cutoff.
    apply     Same as plan, but commits inside a transaction after backing up the DB.

Run `inspect` first, eyeball the recommended cutoff against the chart, then
`plan --cutoff <ts>`, then `apply --cutoff <ts> --yes` with the dev server stopped.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _load_dotenv() -> None:
    """Populate os.environ from ./.env so app.core.config's os.getenv() checks
    see what `make dev` would see. Pydantic loads .env into the Settings object
    but `_validate_security_invariants` reads the raw process env directly.
    """
    env_path = Path(".env")
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        os.environ.setdefault(key, val)


_load_dotenv()

from sqlmodel import Session, select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.db import engine  # noqa: E402
from app.models.db import QuotaSnapshot, UsageWindow  # noqa: E402

PROVIDER = "chatgpt"
DEFAULT_START = datetime(2026, 5, 15, tzinfo=UTC)
DEFAULT_LOW_THRESHOLD = 50.0
DEFAULT_SUSTAIN_MINUTES = 120
DEV_SERVER_HOST = "127.0.0.1"


@dataclass
class RepairUpdate:
    snapshot_id: int
    ts: datetime
    old_pct: float | None
    old_reset_at: datetime | None
    new_pct: float
    new_reset_at: datetime | None
    donor_ts: datetime | None
    fallback: bool


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes for our UTCDateTime columns — coerce to
    tz-aware UTC so comparisons with CLI-parsed datetimes don't trip
    `can't compare offset-naive and offset-aware datetimes`."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def fetch_snapshots(
    session: Session, start: datetime, end: datetime
) -> list[QuotaSnapshot]:
    rows = list(
        session.exec(
            select(QuotaSnapshot)
            .where(
                QuotaSnapshot.provider_id == PROVIDER,
                QuotaSnapshot.ts >= start,
                QuotaSnapshot.ts <= end,
            )
            .order_by(QuotaSnapshot.ts)  # type: ignore[arg-type]
        ).all()
    )
    for r in rows:
        ts = _as_utc(r.ts)
        assert ts is not None
        r.ts = ts
        r.reset_at = _as_utc(r.reset_at)
    return rows


def fetch_closed_windows(
    session: Session, start: datetime, end: datetime
) -> list[UsageWindow]:
    rows = list(
        session.exec(
            select(UsageWindow)
            .where(
                UsageWindow.provider_id == PROVIDER,
                UsageWindow.window_end > start,
                UsageWindow.window_end <= end,
            )
            .order_by(UsageWindow.window_end)  # type: ignore[arg-type]
        ).all()
    )
    for r in rows:
        start_aware = _as_utc(r.window_start)
        end_aware = _as_utc(r.window_end)
        assert start_aware is not None and end_aware is not None
        r.window_start = start_aware
        r.window_end = end_aware
    return rows


def recommend_cutoff(
    snapshots: list[QuotaSnapshot],
    low_threshold: float,
    sustain: timedelta,
) -> datetime | None:
    """Walk snapshots in order and return the ts of the latest high→low transition
    that stays low for `sustain` after the transition.

    Snapshots are grouped by identity (variant, model_id) so a model-specific
    aggregate column doesn't dilute the signal — though for ChatGPT they're all
    the same tuple. Returns None if no sustained drop is found.
    """
    by_identity: dict[tuple[str, str, str], list[QuotaSnapshot]] = {}
    for row in snapshots:
        key = (row.account_id, row.variant, row.model_id)
        by_identity.setdefault(key, []).append(row)

    candidates: list[datetime] = []
    for series in by_identity.values():
        for i in range(1, len(series)):
            prev, curr = series[i - 1], series[i]
            if prev.pct_used is None or curr.pct_used is None:
                continue
            if prev.pct_used < low_threshold or curr.pct_used >= low_threshold:
                continue
            deadline = curr.ts + sustain
            stayed_low = all(
                (r.pct_used is not None and r.pct_used < low_threshold)
                for r in series[i:]
                if r.ts <= deadline
            )
            if stayed_low:
                candidates.append(curr.ts)
    return max(candidates) if candidates else None


def build_repair_plan(
    snapshots: list[QuotaSnapshot],
    cutoff: datetime,
    low_threshold: float,
) -> list[RepairUpdate]:
    """For each snapshot before `cutoff` with pct_used < threshold, find the
    nearest preceding healthy donor (same identity) and propose an UPDATE.
    """
    by_identity: dict[tuple[str, str, str], list[QuotaSnapshot]] = {}
    for row in snapshots:
        key = (row.account_id, row.variant, row.model_id)
        by_identity.setdefault(key, []).append(row)

    updates: list[RepairUpdate] = []
    for series in by_identity.values():
        for i, row in enumerate(series):
            if row.ts >= cutoff:
                continue
            if row.pct_used is None or row.pct_used >= low_threshold:
                continue
            donor: QuotaSnapshot | None = None
            for j in range(i - 1, -1, -1):
                cand = series[j]
                if cand.pct_used is not None and cand.pct_used >= low_threshold:
                    donor = cand
                    break
            if donor is not None:
                assert row.id is not None
                assert donor.pct_used is not None
                updates.append(
                    RepairUpdate(
                        snapshot_id=row.id,
                        ts=row.ts,
                        old_pct=row.pct_used,
                        old_reset_at=row.reset_at,
                        new_pct=donor.pct_used,
                        new_reset_at=donor.reset_at,
                        donor_ts=donor.ts,
                        fallback=False,
                    )
                )
            else:
                assert row.id is not None
                updates.append(
                    RepairUpdate(
                        snapshot_id=row.id,
                        ts=row.ts,
                        old_pct=row.pct_used,
                        old_reset_at=row.reset_at,
                        new_pct=100.0,
                        new_reset_at=None,
                        donor_ts=None,
                        fallback=True,
                    )
                )
    return updates


def fmt_ts(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M")


def cmd_inspect(args: argparse.Namespace) -> int:
    start = args.start
    end = args.end or datetime.now(UTC)
    with Session(engine) as session:
        snapshots = fetch_snapshots(session, start, end)
        windows = fetch_closed_windows(session, start, end)

    print(f"DB: {settings.DATABASE_PATH}")
    print(f"Range: {fmt_ts(start)} → {fmt_ts(end)} UTC")
    print(f"Provider: {PROVIDER}\n")

    print(f"quota_snapshots ({len(snapshots)} rows):")
    print(f"  {'ts (UTC)':<17} {'pct':>6}  {'reset_at':<17} variant / model")
    suspect = 0
    for row in snapshots:
        flag = " "
        if row.pct_used is not None and row.pct_used < args.low_threshold:
            flag = "*"
            suspect += 1
        print(
            f"  {flag} {fmt_ts(row.ts):<17}"
            f" {(row.pct_used if row.pct_used is not None else float('nan')):>6.2f}"
            f"  {fmt_ts(row.reset_at):<17}"
            f" {row.variant or '-'}/{row.model_id or '-'}"
        )
    print(f"\n  * = pct_used below threshold ({args.low_threshold}); {suspect} suspect rows\n")

    print(f"usage_windows ({len(windows)} rows in range):")
    if not windows:
        print("  (none — no window closures during the range)")
    for w in windows:
        print(
            f"  id={w.id} {fmt_ts(w.window_start)} → {fmt_ts(w.window_end)}"
            f" type={w.window_type} model={w.model_id or '-'} sidecar={w.sidecar_id or '-'}"
            f" msgs={w.msgs} tok_in={w.tokens_input} tok_out={w.tokens_output}"
        )
    print()

    cutoff = recommend_cutoff(
        snapshots,
        args.low_threshold,
        timedelta(minutes=args.sustain_minutes),
    )
    if cutoff is None:
        print("No sustained high→low transition found — cannot recommend a cutoff.")
        print("Pass --cutoff explicitly or widen --start/--end.")
        return 0
    print(
        f"Recommended --cutoff: {cutoff.isoformat()}\n"
        f"  (latest high→low transition staying low for ≥{args.sustain_minutes}m)\n"
        f"Next:  python scripts/repair_chatgpt_oscillation.py plan"
        f" --cutoff {cutoff.isoformat()}"
    )
    return 0


def cmd_plan(args: argparse.Namespace, *, label: str = "plan") -> int:
    start = args.start
    cutoff = args.cutoff
    with Session(engine) as session:
        snapshots = fetch_snapshots(session, start, cutoff + timedelta(days=1))
        windows_to_delete = fetch_closed_windows(session, start, cutoff)
        updates = build_repair_plan(snapshots, cutoff, args.low_threshold)

    print(f"DB: {settings.DATABASE_PATH}")
    print(f"Mode: {label}")
    print(f"Window: {fmt_ts(start)} → {fmt_ts(cutoff)} UTC (cutoff exclusive)\n")

    print(f"quota_snapshots updates ({len(updates)}):")
    if not updates:
        print("  (none)")
    for u in updates:
        marker = "FALLBACK" if u.fallback else f"donor@{fmt_ts(u.donor_ts)}"
        print(
            f"  id={u.snapshot_id} ts={fmt_ts(u.ts)}"
            f"  pct {u.old_pct:>6.2f} → {u.new_pct:>6.2f}"
            f"  reset_at {fmt_ts(u.old_reset_at)} → {fmt_ts(u.new_reset_at)}"
            f"  ({marker})"
        )

    print(f"\nusage_windows deletions ({len(windows_to_delete)}):")
    if not windows_to_delete:
        print("  (none)")
    for w in windows_to_delete:
        print(
            f"  id={w.id} {fmt_ts(w.window_start)} → {fmt_ts(w.window_end)}"
            f" type={w.window_type} model={w.model_id or '-'} sidecar={w.sidecar_id or '-'}"
            f" msgs={w.msgs}"
        )
    return 0


def _dev_server_running() -> bool:
    try:
        with socket.create_connection((DEV_SERVER_HOST, settings.APP_PORT), timeout=0.5):
            return True
    except OSError:
        return False


def cmd_apply(args: argparse.Namespace) -> int:
    if not args.yes:
        # Without --yes, behave exactly like `plan`. Print, then exit without writing.
        cmd_plan(args, label="apply (dry run — pass --yes to commit)")
        print("\nNo changes written. Re-run with --yes to commit.")
        return 0

    if _dev_server_running():
        print(
            f"ERROR: dev server is responding on {DEV_SERVER_HOST}:{settings.APP_PORT}.\n"
            "Stop it before running --apply --yes (SQLite is single-writer).",
            file=sys.stderr,
        )
        return 2

    backup_suffix = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = f"{settings.DATABASE_PATH}.bak-{backup_suffix}"
    shutil.copy2(settings.DATABASE_PATH, backup_path)
    print(f"Backed up DB → {backup_path}")

    start = args.start
    cutoff = args.cutoff
    with Session(engine) as session:
        snapshots = fetch_snapshots(session, start, cutoff + timedelta(days=1))
        windows_to_delete = fetch_closed_windows(session, start, cutoff)
        updates = build_repair_plan(snapshots, cutoff, args.low_threshold)

        snap_by_id = {s.id: s for s in snapshots if s.id is not None}
        for u in updates:
            row = snap_by_id[u.snapshot_id]
            row.pct_used = u.new_pct
            row.reset_at = u.new_reset_at
            session.add(row)

        for w in windows_to_delete:
            session.delete(w)

        session.commit()

    print(f"Applied: {len(updates)} snapshot updates, {len(windows_to_delete)} window deletions.")
    print(f"Rollback: stop dev server, then `cp {backup_path} {settings.DATABASE_PATH}`.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument(
        "--start",
        type=parse_ts,
        default=DEFAULT_START,
        help=f"ISO start of the inspection range (default {DEFAULT_START.isoformat()}).",
    )
    parser.add_argument(
        "--low-threshold",
        type=float,
        default=DEFAULT_LOW_THRESHOLD,
        help=f"pct_used below this is treated as suspect (default {DEFAULT_LOW_THRESHOLD}).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_inspect = sub.add_parser("inspect", help="List snapshots + windows in range; recommend cutoff.")
    p_inspect.add_argument(
        "--end",
        type=parse_ts,
        default=None,
        help="ISO end of the inspection range (default: now).",
    )
    p_inspect.add_argument(
        "--sustain-minutes",
        type=int,
        default=DEFAULT_SUSTAIN_MINUTES,
        help=(
            f"Drop must stay low for at least this many minutes to count as the genuine reset"
            f" (default {DEFAULT_SUSTAIN_MINUTES})."
        ),
    )
    p_inspect.set_defaults(func=cmd_inspect)

    p_plan = sub.add_parser("plan", help="Show exact UPDATEs / DELETEs without writing.")
    p_plan.add_argument("--cutoff", type=parse_ts, required=True, help="ISO cutoff timestamp.")
    p_plan.set_defaults(func=cmd_plan)

    p_apply = sub.add_parser("apply", help="Apply repair inside a transaction after DB backup.")
    p_apply.add_argument("--cutoff", type=parse_ts, required=True, help="ISO cutoff timestamp.")
    p_apply.add_argument("--yes", action="store_true", help="Required to actually commit changes.")
    p_apply.set_defaults(func=cmd_apply)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
