#!/usr/bin/env python3
"""Merge the legacy `default` antigravity account into the canonical email account.

Background
----------
After the antigravity-raw-model-wins fix (#307) and the recost + reclassify
post-merge plan, every `default`-account usage_event has an `email`-account
twin by `(provider_id, event_id)` — same logical message, ingested twice
under two account_ids. The naive `default`→email retag violates
`UNIQUE(provider_id, account_id, event_id)`. So we merge by:

  Phase DEDUP — delete every `default`-account `usage_events` row that has an
                email twin by event_id. Email is the canonical side, so no
                cost correction is needed: where the two sides differ on
                `model_id` (raw vs normalized), the email side carries the
                canonical name and the correct cost for it.
  Phase C     — rebuild `usage_period_rollup` from the remaining events.
                Delegated to `recost_events.py:phase_c_rollups`.
  Phase D     — rebuild `usage_windows` from the surviving email-account
                identities. Delegated to `recost_events.py:phase_d_windows`.
  Phase F     — leave `latest_usage` and `quota_snapshots` alone — email-keyed
                live data must not be wiped. Default-keyed gauge rows are a
                hard gate (the merge aborts unless there are zero of them).

Run with the server STOPPED (SQLite is single-writer) and APP_HOST=127.0.0.1:

  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
      python scripts/merge_antigravity_accounts.py --dry-run
  # eyeball counts, then:
  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
      python scripts/merge_antigravity_accounts.py --apply

Exit codes:
  0  success (apply) or pre-flight only (dry-run)
  1  no non-`default` antigravity account found (also tried `latest_usage`/
     `quota_snapshots` fallback)
  2  ambiguous multi-account discovery
  3  default-account `latest_usage`/`quota_snapshots` rows present (gauge gate)
  4  twin pairs diverge on tokens or cost_usd (divergence gate) — only on
     `--apply`; `--dry-run` warns and continues so the pre-flight remains
     runnable

Divergence gate detail (round-3 + round-4):
  Blocking fields: tokens_input/output/cache_read/cache_create/
  cache_create_1h/cache_create_5m/reasoning, cost_usd. Pair count is
  DISTINCT event_ids (not per-field sums) so the abort message matches
  reality. `model_id` / `ts` divergences stay print-only — they're the
  documented `agy` reclassify artifact (raw vs canonical model name) where
  the email side carries the canonical answer.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import NamedTuple

# Fields whose divergence between a `default` row and its email twin means the
# deleted row may carry usage the survivor lacks. `model_id` / `ts` divergences
# stay print-only — they're the documented `agy` reclassify artifact (raw vs
# canonical model name) where the email side carries the canonical answer.
# `tokens_cache_create_1h` / `_5m` are usage-bearing (2x / 1.25x base input
# multiplier; round-4 finding) — included alongside `_cache_create`.
_BLOCKING_DIVERGENCE_FIELDS = (
    "tokens_input",
    "tokens_output",
    "tokens_cache_read",
    "tokens_cache_create",
    "tokens_cache_create_1h",
    "tokens_cache_create_5m",
    "tokens_reasoning",
    "cost_usd",
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_SCRIPTS_DIR = str(_REPO_ROOT / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from sqlmodel import Session, delete, select  # noqa: E402

from app.core.db import engine  # noqa: E402
from app.models.db import (  # noqa: E402
    LatestUsage,
    QuotaSnapshot,
    UsageEvent,
    UsagePeriodRollup,
)
from scripts.recost_events import phase_c_rollups, phase_d_windows  # noqa: E402

_PROVIDER = "antigravity"
_LEGACY_ACCOUNT = "default"


def _discover_canonical_account(session: Session) -> str:
    """Return the (only) non-`default` antigravity account_id, or abort.

    Fails loudly if more than one non-`default` account exists — with two,
    picking one arbitrarily would silently leak `default` rows twinned with
    the other as orphans.

    Falls back to `latest_usage` / `quota_snapshots` when no non-`default`
    account appears in `usage_events` (Hermes round-3 finding) — the
    LSP-only host shape `cleanup_antigravity.py` Phase A targets.
    """
    rows = session.exec(
        select(UsageEvent.account_id)
        .where(UsageEvent.provider_id == _PROVIDER)
        .where(UsageEvent.account_id != _LEGACY_ACCOUNT)
        .distinct()
    ).all()
    source = "usage_events"
    if not rows:
        rows = session.exec(
            select(LatestUsage.account_id)
            .where(LatestUsage.provider_id == _PROVIDER)
            .where(LatestUsage.account_id != _LEGACY_ACCOUNT)
            .distinct()
        ).all()
        source = "latest_usage"
    if not rows:
        # Final fallback: the gauge-only host whose canonical email appears
        # only in `quota_snapshots` (e.g. a brand-new account where the
        # `latest_usage` upsert hasn't fired yet). Round-4 finding: previous
        # version advertised this fallback but never queried it.
        rows = session.exec(
            select(QuotaSnapshot.account_id)
            .where(QuotaSnapshot.provider_id == _PROVIDER)
            .where(QuotaSnapshot.account_id != _LEGACY_ACCOUNT)
            .distinct()
        ).all()
        source = "quota_snapshots"
    if not rows:
        print(
            "No non-`default` antigravity account found in `usage_events` "
            "(or `latest_usage`/`quota_snapshots` fallback) — nothing to merge into.",
            file=sys.stderr,
        )
        sys.exit(1)
    if len(rows) > 1:
        print(
            f"Found {len(rows)} non-`default` antigravity accounts (via {source}): {rows!r}. "
            "Merge target is ambiguous; aborting.",
            file=sys.stderr,
        )
        sys.exit(2)
    return rows[0]


def _count_default_message_events(session: Session) -> tuple[int, int]:
    """Return (default message events, default message events with an email twin by event_id).

    Filters consistently with `phase_dedup_delete` (kind='message') so the
    headline number agrees with what DEDUP actually does. Non-message
    default events are surfaced separately by `_count_default_non_message`.
    """
    n_default = session.exec(
        select(UsageEvent)
        .where(UsageEvent.provider_id == _PROVIDER)
        .where(UsageEvent.account_id == _LEGACY_ACCOUNT)
        .where(UsageEvent.kind == "message")
    ).all()
    twin_ids = session.exec(
        select(UsageEvent.event_id)
        .where(UsageEvent.provider_id == _PROVIDER)
        .where(UsageEvent.account_id != _LEGACY_ACCOUNT)
        .where(UsageEvent.kind == "message")
    ).all()
    twin_set = set(twin_ids)
    n_with_twin = sum(1 for ev in n_default if ev.event_id in twin_set)
    return len(n_default), n_with_twin


def _count_default_non_message(session: Session) -> int:
    """Return the number of default-account events whose kind != 'message'."""
    return len(
        session.exec(
            select(UsageEvent)
            .where(UsageEvent.provider_id == _PROVIDER)
            .where(UsageEvent.account_id == _LEGACY_ACCOUNT)
            .where(UsageEvent.kind != "message")
        ).all()
    )


class _DivergenceResult(NamedTuple):
    """Combined twin-divergence scan result.

    Round-5 finding: keeping `_count_twin_divergence` and
    `_find_blocking_divergent_event_ids` as two functions that each ran
    their own row query risked drift between the printed scan and the
    gate that acts on it. One pass now returns both: the per-field counts
    (for the print) and the distinct event_ids that diverge on any
    blocking field (for the gate).
    """

    per_field_counts: dict[str, int]
    divergent_event_ids: list[str]


def _scan_twin_divergence(session: Session, email: str) -> _DivergenceResult:
    """Single pass: returns per-field counts AND the distinct event_ids of
    twin pairs that diverge on any blocking field.

    Useful when the dual-ingest paths differ — e.g. `agy` raw `model_id`
    vs canonical name, or timestamp drift between two collectors. The
    caller prints `per_field_counts` for the operator and acts on
    `divergent_event_ids` for the gate.

    Reads only.
    """
    fields = (
        "model_id",
        "ts",
        "tokens_input",
        "tokens_output",
        "tokens_cache_read",
        "tokens_cache_create",
        "tokens_cache_create_1h",
        "tokens_cache_create_5m",
        "tokens_reasoning",
        "cost_usd",
    )
    sql_cols = [getattr(UsageEvent, f) for f in fields]
    sql = select(UsageEvent.event_id, *sql_cols).where(
        UsageEvent.provider_id == _PROVIDER,
        UsageEvent.account_id == _LEGACY_ACCOUNT,
        UsageEvent.kind == "message",
    )
    default_rows = session.exec(sql).all()
    twin_sql = select(UsageEvent.event_id, *sql_cols).where(
        UsageEvent.provider_id == _PROVIDER,
        UsageEvent.account_id == email,
        UsageEvent.kind == "message",
    )
    twin_rows = {row[0]: row for row in session.exec(twin_sql).all()}

    per_field: dict[str, int] = dict.fromkeys(fields, 0)
    per_field["any"] = 0
    divergent: list[str] = []
    for drow in default_rows:
        eid = drow[0]
        trow = twin_rows.get(eid)
        if trow is None:
            continue
        blocking_diff = False
        any_diff = False
        for i, field in enumerate(fields, start=1):
            if drow[i] != trow[i]:
                per_field[field] += 1
                any_diff = True
                if field in _BLOCKING_DIVERGENCE_FIELDS:
                    blocking_diff = True
        if any_diff:
            per_field["any"] += 1
        if blocking_diff:
            divergent.append(eid)
    return _DivergenceResult(per_field_counts=per_field, divergent_event_ids=divergent)


def _count_default_rollups(session: Session) -> int:
    return len(
        session.exec(
            select(UsagePeriodRollup)
            .where(UsagePeriodRollup.provider_id == _PROVIDER)
            .where(UsagePeriodRollup.account_id == _LEGACY_ACCOUNT)
        ).all()
    )


class _DedupResult(NamedTuple):
    """Return shape for `phase_dedup_delete`.

    Round-5 finding: returning `dict[str, object]` left the call site
    (`surviving_ids - dedup_counts.delete_ids`) untypecheckable.
    A NamedTuple gives the call site attribute access with full
    type info (per-field `int` + the `set[int]` delete_ids).
    """

    default_message_events_total: int
    default_events_with_email_twin: int
    default_events_orphan_no_twin: int
    delete_ids: set[int]


def phase_dedup_delete(session: Session, email: str, dry_run: bool) -> _DedupResult:
    """Delete legacy-account `message` events whose event_id has an email twin.

    Every default-account message event in the current DB has an email
    twin by event_id (verified in dry-run); we delete them all and keep
    the email row. The `UNIQUE(provider_id, account_id, event_id)` constraint
    means a retag would fail; deletion avoids that.

    Non-message default events are left for an explicit follow-up.
    """
    email_event_ids = set(
        session.exec(
            select(UsageEvent.event_id)
            .where(UsageEvent.provider_id == _PROVIDER)
            .where(UsageEvent.account_id == email)
            .where(UsageEvent.kind == "message")
        ).all()
    )
    default_events = session.exec(
        select(UsageEvent)
        .where(UsageEvent.provider_id == _PROVIDER)
        .where(UsageEvent.account_id == _LEGACY_ACCOUNT)
        .where(UsageEvent.kind == "message")
    ).all()
    to_delete = [ev for ev in default_events if ev.event_id in email_event_ids]
    skipped_orphan = len(default_events) - len(to_delete)
    delete_ids = {ev.id for ev in to_delete if ev.id is not None}

    verb = "Would delete" if dry_run else "Deleting"
    print(
        f"Phase DEDUP — {verb} {len(to_delete):,} {repr(_LEGACY_ACCOUNT)}-account "
        f"usage_events (kind='message') with an {repr(email)} twin by event_id. "
        f"Orphans left alone: {skipped_orphan:,}.",
        flush=True,
    )

    if not dry_run and to_delete:
        BATCH = 1000
        ids = sorted(delete_ids)
        total = 0
        for i in range(0, len(ids), BATCH):
            chunk = ids[i : i + BATCH]
            session.exec(
                delete(UsageEvent)
                .where(UsageEvent.provider_id == _PROVIDER)
                .where(UsageEvent.account_id == _LEGACY_ACCOUNT)
                .where(UsageEvent.kind == "message")
                .where(UsageEvent.id.in_(chunk))  # type: ignore[attr-defined]
            )
            session.commit()
            total += len(chunk)
            print(f"  …deleted {total:,}/{len(to_delete):,}", flush=True)

    return _DedupResult(
        default_message_events_total=len(default_events),
        default_events_with_email_twin=len(to_delete),
        default_events_orphan_no_twin=skipped_orphan,
        delete_ids=delete_ids,
    )


def phase_f_gauge_unchanged(session: Session, email: str) -> dict[str, int]:
    """Report the live-gauge state; default-keyed rows are a hard gate.

    Email-keyed live data must survive the merge. Default-keyed gauge
    rows are NOT touched here — they're a precondition that the merge
    refuses to proceed past. (A future cleanup can remove them; today the
    expected state is zero.)
    """
    n_cards = session.exec(
        select(LatestUsage)
        .where(LatestUsage.provider_id == _PROVIDER)
        .where(LatestUsage.account_id == email)
    ).all()
    n_snaps = session.exec(
        select(QuotaSnapshot)
        .where(QuotaSnapshot.provider_id == _PROVIDER)
        .where(QuotaSnapshot.account_id == email)
    ).all()
    n_default_cards = session.exec(
        select(LatestUsage)
        .where(LatestUsage.provider_id == _PROVIDER)
        .where(LatestUsage.account_id == _LEGACY_ACCOUNT)
    ).all()
    n_default_snaps = session.exec(
        select(QuotaSnapshot)
        .where(QuotaSnapshot.provider_id == _PROVIDER)
        .where(QuotaSnapshot.account_id == _LEGACY_ACCOUNT)
    ).all()
    print(
        "Phase F (gauge) — "
        f"email latest_usage cards preserved: {len(n_cards):,} | "
        f"email quota_snapshots preserved: {len(n_snaps):,} | "
        f"default latest_usage rows (HARD GATE): {len(n_default_cards):,} | "
        f"default quota_snapshots rows (HARD GATE): {len(n_default_snaps):,}",
        flush=True,
    )
    return {
        "email_latest_usage": len(n_cards),
        "email_quota_snapshots": len(n_snaps),
        "default_latest_usage": len(n_default_cards),
        "default_quota_snapshots": len(n_default_snaps),
    }


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="Report only.")
    g.add_argument("--apply", action="store_true", help="Apply all destructive writes.")
    p.add_argument(
        "--force",
        action="store_true",
        help=(
            "Bypass both the default-keyed live-gauge gate (latest_usage / "
            "quota_snapshots, rc=3) and the twin-divergence gate (rc=4). "
            "Use only after manual cleanup / inspection. The script still "
            "reports both gate failures before deletion."
        ),
    )
    args = p.parse_args()
    dry_run = args.dry_run
    prefix = "[DRY-RUN] " if dry_run else ""

    with Session(engine) as session:
        email = _discover_canonical_account(session)
        print(f"{prefix}Canonical email: {email!r}", flush=True)

        n_default, n_with_twin = _count_default_message_events(session)
        n_default_non_message = _count_default_non_message(session)
        print(
            f"{prefix}Default-account usage_events (kind='message'): {n_default:,} "
            f"(with email twin by event_id: {n_with_twin:,})",
            flush=True,
        )
        if n_default_non_message:
            print(
                f"{prefix}Default-account usage_events (kind != 'message'): "
                f"{n_default_non_message:,} — left alone; not part of the merge.",
                flush=True,
            )

        # Hermes round-5: single-pass scan now returns both the per-field
        # counts (for the print) and the distinct divergent event_ids (for
        # the gate). The print and the gate no longer drift.
        divergence = _scan_twin_divergence(session, email)
        diverge = divergence.per_field_counts
        print(
            f"{prefix}Twin divergence scan ({n_with_twin:,} pairs): "
            f"model_id={diverge['model_id']:,} | ts={diverge['ts']:,} | "
            f"tokens_input={diverge['tokens_input']:,} | tokens_output={diverge['tokens_output']:,} | "
            f"tokens_cache_read={diverge['tokens_cache_read']:,} | "
            f"tokens_cache_create={diverge['tokens_cache_create']:,} | "
            f"tokens_cache_create_1h={diverge['tokens_cache_create_1h']:,} | "
            f"tokens_cache_create_5m={diverge['tokens_cache_create_5m']:,} | "
            f"tokens_reasoning={diverge['tokens_reasoning']:,} | "
            f"cost_usd={diverge['cost_usd']:,} | ANY={diverge['any']:,}",
            flush=True,
        )

        # Hermes round-3 finding: divergence scan was print-only, but the PR
        # body's "nothing to correct" claim rests on the invariant that no
        # blocking field (tokens / cost) diverges. Enforce it here.
        # Hermes round-4: count DISTINCT event_ids (not per-field sums), include
        # the offending event_ids so the operator can act on the abort, and
        # don't trip the gate in dry-run (the operator's only pre-flight must
        # remain runnable).
        divergent_event_ids = divergence.divergent_event_ids
        if divergent_event_ids:
            sample = ", ".join(divergent_event_ids[:5])
            more = (
                f" (and {len(divergent_event_ids) - 5} more)"
                if len(divergent_event_ids) > 5
                else ""
            )
            print(
                f"\n{prefix}TWIN DIVERGENCE WARNING — "
                f"{len(divergent_event_ids):,} twin pair(s) disagree on "
                "tokens or cost; the deleted `default` row may carry usage "
                f"the survivor lacks. Sample event_ids: {sample}{more}.",
                file=sys.stderr,
                flush=True,
            )
            if not dry_run and not args.force:
                print(
                    f"{prefix}Aborting with rc=4. Re-run with --force only "
                    "after manual inspection.",
                    file=sys.stderr,
                    flush=True,
                )
                sys.exit(4)

        n_default_rollups = _count_default_rollups(session)
        print(
            f"{prefix}Default-account usage_period_rollup rows: {n_default_rollups:,} "
            "(Phase C will rebuild all antigravity rollups from final events.)",
            flush=True,
        )

        gauge = phase_f_gauge_unchanged(session, email)

        # Round-5 finding: dry-run used to trip this gate too, so the only
        # runnable pre-flight was --force. Same fix as the divergence gate:
        # print a warning in dry-run, abort on apply.
        if gauge["default_latest_usage"] or gauge["default_quota_snapshots"]:
            print(
                f"\n{prefix}GAUGE GATE WARNING — default-account latest_usage "
                f"({gauge['default_latest_usage']:,}) or quota_snapshots "
                f"({gauge['default_quota_snapshots']:,}) rows exist. They "
                "would still surface as a `default` account identity in "
                "fleet views after the merge.",
                file=sys.stderr,
                flush=True,
            )
            if not dry_run and not args.force:
                print(
                    f"{prefix}Aborting with rc=3. Re-run with --force only "
                    "after manually cleaning those rows.",
                    file=sys.stderr,
                    flush=True,
                )
                sys.exit(3)

        dedup_counts = phase_dedup_delete(session, email, dry_run)

        verb = "rebuilding" if not dry_run else "previewing"
        verb_done = "rebuilt" if not dry_run else "would rebuild"
        print(f"{prefix}Phase C — {verb} antigravity rollups from events…", flush=True)
        if dry_run:
            # Hermes round-3 finding: phase_c_rollups(dry_run=True) counts
            # events in the live DB — still includes the about-to-be-deleted
            # `default` rows. The operator's only pre-flight must reflect the
            # post-DEDUP state, so recompute here.
            surviving_ids = set(
                session.exec(
                    select(UsageEvent.id).where(
                        UsageEvent.provider_id == _PROVIDER,
                        UsageEvent.kind == "message",
                    )
                ).all()
            )
            n_events = len(surviving_ids - dedup_counts.delete_ids)
        else:
            n_events = phase_c_rollups(session, [_PROVIDER], dry_run)
        print(
            f"{prefix}Phase C done — rollups {verb_done} from {n_events:,} event(s).",
            flush=True,
        )

        print(f"{prefix}Phase D — {verb} antigravity windows…", flush=True)
        n_windows = phase_d_windows(session, [_PROVIDER], dry_run)
        print(
            f"{prefix}Phase D done — {n_windows:,} window identity(-ies) {verb_done}.",
            flush=True,
        )

    print(f"\n{prefix}Summary for {_PROVIDER!r} merge into {email!r}:")
    print(
        f"  Default-account events deleted (with email twin): {dedup_counts.default_events_with_email_twin:,}"
    )
    print(
        f"  Default-account events left (no email twin)     : {dedup_counts.default_events_orphan_no_twin:,}"
    )
    print(f"  Default-account non-message events (left alone) : {n_default_non_message:,}")
    print(f"  Default-account rollup rows (deleted by Phase C): {n_default_rollups:,}")
    print(
        f"  Events used to {'rebuild' if not dry_run else 'preview'} rollups (Phase C)        : {n_events:,}"
    )
    print(
        f"  Window identities {'rebuilt' if not dry_run else 'previewed'} (Phase D)             : {n_windows:,}"
    )
    print(f"  Email latest_usage cards PRESERVED              : {gauge['email_latest_usage']:,}")
    print(f"  Email quota_snapshots PRESERVED                 : {gauge['email_quota_snapshots']:,}")
    if args.force:
        print(
            f"  Default latest_usage rows (--force, untouched)  : {gauge['default_latest_usage']:,}"
        )
        print(
            f"  Default quota_snapshots rows (--force, untouched): {gauge['default_quota_snapshots']:,}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
