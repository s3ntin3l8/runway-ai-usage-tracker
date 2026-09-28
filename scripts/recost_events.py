#!/usr/bin/env python3
"""Recompute cost_usd on usage_events and rebuild derived cost tables.

Use after a provider_pricing seed change so that existing events pick up the
new rates. Run once after upgrading to populate the new reported and estimated
cost columns for existing events. Stop the server before running so SQLite has
one writer. Up to three passes run in sequence:

  Phase B — update usage_events.cost_usd where the recomputed value differs.
  Phase C — delete and rebuild usage_period_rollup for the (provider_id,
            account_id) pairs Phase B actually changed something for — a
            subset of the CLI-supplied provider scope, not the whole scope.
  Phase D — delete and rebuild usage_windows for the providers touched by
            those same pairs — likewise a subset of the CLI-supplied scope.

If Phase B finds no cost changes at all, Phases C and D are skipped entirely
(there's nothing to rebuild). This means `--all` on an already-correctly-
priced database is now a cheap no-op instead of an unconditional full rebuild
of every provider's rollups/windows. For a genuine drift repair — rollups or
windows that disagree with usage_events for reasons other than a price
change — use the Data Health `rollup_drift` check
(app/services/data_health/checks/rollup_drift.py) instead; this script's job
is re-pricing, not general-purpose "rebuild everything just in case".

This is a thin CLI wrapper — the actual logic lives in
app/services/maintenance/{event_cost,recost,windows}.py and
app/services/period_rollups.py, shared with the in-app Data Health
`unpriced_models` fixer so a host-run script and an in-app fix can never
drift apart.

Source-reported amounts are migrated into cost_reported_usd and kept separate
from calculated token value. Account billing_type selects the shown total:
subscriptions and any account whose model has a resolvable price row use the
computed estimate; pay-as-you-go accounts, and any account whose model has no
resolvable price row at all, use the reported amount when available — see
app/services/maintenance/event_cost.py for the full rule (this replaces an
older, OpenCode-specific "unknown billing type" carve-out with one general
rule that applies to every provider). Error events are skipped.

Note on effective_from: cost_calculator only applies a pricing row when
effective_from <= event.ts.date(). If the new seed rows are dated today,
events from before today will still compute to 0.0 for those model_ids
unless you also backdate effective_from in pricing_seed.py.

Examples:
  # Recompute chatgpt only
  python scripts/recost_events.py --provider chatgpt

  # Preview without writing
  python scripts/recost_events.py --provider chatgpt --dry-run

  # Recompute several providers, skipping the window archive rebuild
  python scripts/recost_events.py --provider chatgpt --provider gemini --skip-windows

  # Recompute all providers, only events from a date onward (Phase B only)
  python scripts/recost_events.py --all --since 2025-08-01

  # Recompute every provider; Phases C/D only rebuild the pairs that
  # actually had a cost change (a no-op if nothing changed)
  python scripts/recost_events.py --all
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session, func, select  # noqa: E402

from app.core.db import engine  # noqa: E402
from app.models.db import UsageEvent  # noqa: E402
from app.services.maintenance.recost import apply_recost, plan_recost  # noqa: E402
from app.services.maintenance.rollups import (  # noqa: E402
    rebuild_rollups_for_pairs,
    rebuild_rollups_for_providers,
)
from app.services.maintenance.windows import (  # noqa: E402
    count_windows_for_providers,
    rebuild_windows_for_providers,
)


def phase_b_recost(
    session: Session,
    providers: list[str] | None,
    since: date | None,
    dry_run: bool,
) -> tuple[int, int, int, set[tuple[str, str]]]:
    """Recompute cost_usd on usage_events.

    Returns (updated, unchanged, zeroed, affected_pairs) — affected_pairs is
    the set of (provider_id, account_id) pairs that actually had a cost
    change, already computed by `plan_recost`/`apply_recost`; Phase C/D use
    it to narrow their own rebuild instead of re-scanning the full CLI
    provider scope.

    This CLI never passes `only_zero_cost=True` (that's the Data Health
    `unpriced_models` fixer's contract, not this script's), so the printed
    total never has a `skipped_still_unpriced` count to exclude — it would
    always be 0 here.
    """
    if dry_run:
        plan = plan_recost(session, providers, since=since, sample_size=0)
        print(
            f"Phase B — examining {plan.updated + plan.unchanged + plan.zeroed:,} event(s)…",
            flush=True,
        )
        return plan.updated, plan.unchanged, plan.zeroed, plan.affected_pairs
    # apply_recost already computes affected_pairs and (when not told to
    # skip) would narrow its own rollup/window rebuild to them — we keep it
    # skipping that here and let Phase C/D below do the rebuild instead, but
    # we still read affected_pairs off the result before discarding the rest.
    result = apply_recost(session, providers, since=since, skip_rollups=True, skip_windows=True)
    print(
        f"Phase B — examined {result.updated + result.unchanged + result.zeroed:,} event(s)…",
        flush=True,
    )
    return result.updated, result.unchanged, result.zeroed, result.affected_pairs


def phase_c_rollups(
    session: Session,
    providers: list[str] | None,
    dry_run: bool,
    pairs: set[tuple[str, str]] | None = None,
) -> int:
    """Rebuild usage_period_rollup. Returns events processed.

    When `pairs` is given (even an empty set), narrow the rebuild to exactly
    those (provider_id, account_id) pairs instead of the full `providers`
    scope — this is how `run()` limits Phase C to what Phase B actually
    changed. `pairs=None` (the default) preserves the old whole-provider-scope
    behavior for standalone callers.
    """
    if pairs is not None:
        if not pairs:
            # Not load-bearing for correctness — `tuple_(...).in_(())` below
            # would already match zero rows on its own — this is purely to
            # skip the round trip and rebuild_rollups_for_pairs call for the
            # common "nothing changed" case. Contrast phase_d_windows, where
            # the equivalent check *is* load-bearing (an empty `providers`
            # list means "every provider" to the functions it calls).
            print("Phase C — rebuilding rollups from 0 event(s)…", flush=True)
            return 0
        from sqlalchemy import tuple_

        stmt = (
            select(func.count())
            .select_from(UsageEvent)
            .where(UsageEvent.kind == "message")
            .where(
                tuple_(UsageEvent.provider_id, UsageEvent.account_id).in_(sorted(pairs))  # type: ignore[arg-type]
            )
        )
        n_events = session.exec(stmt).one()
        print(f"Phase C — rebuilding rollups from {n_events:,} event(s)…", flush=True)
        if not dry_run:
            rebuild_rollups_for_pairs(session, pairs)
        return n_events

    stmt = select(func.count()).select_from(UsageEvent).where(UsageEvent.kind == "message")
    if providers:
        stmt = stmt.where(UsageEvent.provider_id.in_(providers))  # type: ignore[attr-defined]
    n_events = session.exec(stmt).one()
    print(f"Phase C — rebuilding rollups from {n_events:,} event(s)…", flush=True)
    if not dry_run:
        rebuild_rollups_for_providers(session, providers)
    return n_events


def phase_d_windows(
    session: Session,
    providers: list[str] | None,
    dry_run: bool,
    pairs: set[tuple[str, str]] | None = None,
) -> int:
    """Rebuild usage_windows. Returns window-identities rebuilt.

    When `pairs` is given (even an empty set), narrow the provider scope to
    `sorted({p for p, _a in pairs})` — the same provider-level narrowing
    `apply_recost` does internally — instead of the full `providers` list.
    `pairs=None` (the default) preserves the old whole-provider-scope
    behavior for standalone callers.
    """
    scope: list[str] | None
    if pairs is not None:
        scope = sorted({p for p, _a in pairs})
        if not scope:
            # An empty `providers` list means "every provider" to
            # count_windows_for_providers/rebuild_windows_for_providers, so
            # an empty pair set must short-circuit here rather than fall
            # through to that call.
            print("Phase D — rebuilding 0 window(s)…", flush=True)
            return 0
    else:
        scope = providers

    if dry_run:
        n_windows = count_windows_for_providers(session, scope)
        print(f"Phase D — rebuilding {n_windows:,} window(s)…", flush=True)
        return n_windows
    n_windows = rebuild_windows_for_providers(session, scope)
    print(f"Phase D — rebuilt {n_windows:,} window(s)…", flush=True)
    return n_windows


def run(
    providers: list[str] | None,
    since: date | None,
    dry_run: bool,
    skip_rollups: bool,
    skip_windows: bool,
) -> None:
    prefix = "[DRY-RUN] " if dry_run else ""
    scope = "all providers" if providers is None else ", ".join(providers)
    print(f"{prefix}Re-costing events for: {scope}", flush=True)

    with Session(engine) as session:
        updated, unchanged, zeroed, affected_pairs = phase_b_recost(
            session, providers, since, dry_run
        )
        print(
            f"{prefix}Phase B done — {updated + unchanged + zeroed:,} event(s): "
            f"{updated} updated, {unchanged} unchanged, {zeroed} newly-zeroed.",
            flush=True,
        )

        if not affected_pairs:
            # Quantify the "would rebuild" preview explicitly rather than
            # just saying "skipped" — this is the accurate answer to what a
            # real run would now do (0 pairs, so nothing), which is a more
            # useful dry-run signal than the old code's whole-provider
            # event/window counts (those previewed the unconditional
            # rebuild this PR removes, so they'd now overstate what a real
            # run actually touches).
            print(
                f"{prefix}No cost changes — Phases C/D skipped "
                "(0 pair(s) affected: 0 event(s), 0 window(s) would be rebuilt).",
                flush=True,
            )
        else:
            if not skip_rollups:
                n_events = phase_c_rollups(session, providers, dry_run, pairs=affected_pairs)
                print(
                    f"{prefix}Phase C done — rollups rebuilt from {n_events:,} event(s).",
                    flush=True,
                )
            else:
                print("Phase C skipped (--skip-rollups).", flush=True)

            if not skip_windows:
                n_windows = phase_d_windows(session, providers, dry_run, pairs=affected_pairs)
                print(f"{prefix}Phase D done — {n_windows:,} window(s) rebuilt.", flush=True)
            else:
                print("Phase D skipped (--skip-windows).", flush=True)


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--provider",
        action="append",
        default=[],
        dest="providers",
        metavar="ID",
        help="Provider id to re-cost (repeatable). Use --all to target every provider.",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="Re-cost events for every provider.",
    )
    p.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help="Only re-cost events on or after this date (Phase B). "
        "Phases C and D only rebuild the (provider_id, account_id) pairs "
        "Phase B actually changed something for — not necessarily every "
        "event/provider in scope.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing anything.",
    )
    p.add_argument(
        "--skip-rollups",
        action="store_true",
        help="Skip Phase C (usage_period_rollup rebuild).",
    )
    p.add_argument(
        "--skip-windows",
        action="store_true",
        help="Skip Phase D (usage_windows rebuild).",
    )
    args = p.parse_args()

    if not args.providers and not args.all:
        p.error("supply --provider <id> (repeatable) or --all")

    since: date | None = None
    if args.since:
        try:
            since = date.fromisoformat(args.since)
        except ValueError:
            p.error(f"--since must be YYYY-MM-DD, got: {args.since!r}")

    providers = args.providers if not args.all else None
    run(
        providers=providers,
        since=since,
        dry_run=args.dry_run,
        skip_rollups=args.skip_rollups,
        skip_windows=args.skip_windows,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
