#!/usr/bin/env python3
"""Repair chatgpt usage_events written before full Codex model-slug preservation.

Background
----------
scripts/sidecar_pkg/event_extractors/chatgpt.py's _normalize_chatgpt_model()
used to collapse every Codex model slug to a bare version or family bucket —
"gpt-5.6-sol" -> "gpt-5.6", every "*-codex*" variant -> "codex" — and never
read reasoning effort (turn_context.payload.effort) at all. The extractor now
preserves the full slug and captures effort; this script re-reads the
on-disk Codex rollout logs with the corrected extractor and repairs rows
already ingested under the old lossy mapping.

Matching is by event_id (`<file_stem>|line_<n>`, stable across replays as
long as the source file is only appended to — see parse_chatgpt_events). Rows
whose source file has since been pruned/rotated are left untouched and
counted separately; there is no other source for their original slug
(raw_json is NULL on these rows).

Also corrects the "gpt-5" pricing row seeded with a wrong rate ($5.00/$15.00
instead of the published $1.25/$10.00 — see app/services/pricing_seed.py).
seed_pricing_table() is insert-only and never touches an existing row, so
editing the literal there only fixes fresh installs; running this script
without --dry-run also UPDATEs the live row so existing DBs pick up the
correction. That write
is off-name for a "reclassify model_id" script, but it rides along here
rather than shipping a second one-line script — flagged explicitly so it
isn't a surprise in a diff.

After repairing model_id/effort, run scripts/recost_events.py --provider
chatgpt to reprice the affected rows from provider_pricing (the corrected
gpt-5 rate and the newly-seeded codename/codex rows).

Run with the server STOPPED (SQLite is single-writer) and APP_HOST=127.0.0.1:

  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
      python scripts/reclassify_chatgpt_models.py --dry-run
  # eyeball the planned changes, then:
  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
      python scripts/reclassify_chatgpt_models.py
  python scripts/recost_events.py --provider chatgpt
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session, select  # noqa: E402

from app.core.console import force_utf8_console  # noqa: E402
from app.core.db import engine, init_db  # noqa: E402
from app.models.db import ProviderPricing, UsageEvent  # noqa: E402
from app.models.schemas import UsageEventPush  # noqa: E402
from scripts.backfill_rollups import backfill as rebuild_rollups  # noqa: E402
from scripts.sidecar import _discover_codex_log_paths  # noqa: E402
from scripts.sidecar_pkg.event_extractors.chatgpt import parse_chatgpt_events  # noqa: E402

# Reach back far enough to cover all retained history.
_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)

# The wrong rate this script repairs (see pricing_seed.py's "gpt-5" row).
_WRONG_GPT5_INPUT = 5.00
_WRONG_GPT5_OUTPUT = 15.00
_WRONG_GPT5_CACHE_READ = 1.25
_CORRECT_GPT5_INPUT = 1.25
_CORRECT_GPT5_OUTPUT = 10.00
_CORRECT_GPT5_CACHE_READ = 0.125


def _collect_pushes() -> dict[str, UsageEventPush]:
    """Re-parse on-disk Codex rollouts -> {event_id: push} under the fixed extractor."""
    pushes = parse_chatgpt_events(_discover_codex_log_paths(), "backfill", _EPOCH)
    return {p.event_id: p for p in pushes}


def _fix_gpt5_pricing_row(session: Session, dry_run: bool) -> bool:
    """Correct the mis-seeded chatgpt/gpt-5 rate in place. Guarded so a rate
    already changed by hand (or already fixed) is never clobbered."""
    row = session.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "chatgpt",
            ProviderPricing.model_id == "gpt-5",
            ProviderPricing.effective_from == date(2025, 8, 1),
        )
    ).first()
    if row is None:
        print("gpt-5 pricing row: not found, nothing to fix.", flush=True)
        return False
    if (
        row.input_per_mtok != _WRONG_GPT5_INPUT
        or row.output_per_mtok != _WRONG_GPT5_OUTPUT
        or row.cache_read_per_mtok != _WRONG_GPT5_CACHE_READ
    ):
        print(
            "gpt-5 pricing row: already at a non-default rate "
            f"(input={row.input_per_mtok}, output={row.output_per_mtok}, "
            f"cache_read={row.cache_read_per_mtok}) — leaving as-is.",
            flush=True,
        )
        return False
    print(
        f"gpt-5 pricing row: {row.input_per_mtok}/{row.cache_read_per_mtok}/"
        f"{row.output_per_mtok} -> {_CORRECT_GPT5_INPUT}/{_CORRECT_GPT5_CACHE_READ}/"
        f"{_CORRECT_GPT5_OUTPUT}" + (" (dry-run)" if dry_run else ""),
        flush=True,
    )
    if not dry_run:
        row.input_per_mtok = _CORRECT_GPT5_INPUT
        row.output_per_mtok = _CORRECT_GPT5_OUTPUT
        row.cache_read_per_mtok = _CORRECT_GPT5_CACHE_READ
        session.add(row)
    return True


def reclassify(session: Session, dry_run: bool) -> int:
    """Repair model_id/effort on existing chatgpt usage_events. Returns rows changed."""
    pushes = _collect_pushes()
    if not pushes:
        print("No Codex rollout logs found; nothing to repair.", flush=True)
        return 0

    rows = session.exec(select(UsageEvent).where(UsageEvent.provider_id == "chatgpt")).all()
    changed = 0
    missing = 0
    for row in rows:
        # event_id is "<file_stem>|line_<n>" — this match is only correct
        # under the append-only invariant real Codex rollout logs hold. A
        # file recreated in place at the same path with the same stem but
        # different content at a given line number would silently attribute
        # that line's (model_id, effort) to the wrong original event, with
        # no detection here — there's no stored hash/mtime of the original
        # content to check against (raw_json is NULL on these rows). The
        # printed per-row diff plus --dry-run is the only safety net; review
        # it before applying against logs of uncertain provenance.
        push = pushes.get(row.event_id)
        if push is None:
            missing += 1
            continue
        new_model_id = push.model_id
        new_effort = push.effort
        if new_model_id == row.model_id and new_effort == row.effort:
            continue
        prefix = "[DRY-RUN] " if dry_run else ""
        print(
            f"{prefix}{row.event_id}: model_id {row.model_id!r} -> {new_model_id!r}, "
            f"effort {row.effort!r} -> {new_effort!r}",
            flush=True,
        )
        if not dry_run:
            row.model_id = new_model_id
            row.effort = new_effort
            session.add(row)
        changed += 1

    if not dry_run and changed:
        session.commit()

    print(
        f"\n{'[DRY-RUN] ' if dry_run else ''}{changed:,} row(s) "
        f"{'would be' if dry_run else ''} repaired ({missing:,} not found in on-disk logs).",
        flush=True,
    )
    return changed


def main() -> int:
    force_utf8_console()
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--dry-run", action="store_true", help="report counts, write nothing")
    args = p.parse_args()

    init_db()
    with Session(engine) as session:
        events_changed = reclassify(session, args.dry_run)
        _fix_gpt5_pricing_row(session, args.dry_run)
        if not args.dry_run:
            session.commit()

    if not args.dry_run and events_changed:
        print("Rebuilding rollups for: chatgpt", flush=True)
        rebuild_rollups(["chatgpt"])

    return 0


if __name__ == "__main__":
    sys.exit(main())
