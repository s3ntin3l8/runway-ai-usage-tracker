#!/usr/bin/env python3
"""Collapse a duplicated Gemini account identity (``default`` -> email).

Background
----------
While Gemini quota was collected server-side in local mode, the server read
``~/.gemini/oauth_creds.json`` directly — ``id_token`` included — and resolved the
canonical email ``account_id`` (see ``app/services/account_identity.py``). When
collection moved to the sidecar-only path, the sidecar's credential mapping
omitted the ``id_token`` (fixed in ``scripts/sidecar.py`` / ``app/core/registry.json``),
so the server could no longer derive the email and fell back to
``account_id="default"``. That spawned a second set of quota cards (one per model)
which the dashboard — grouping by ``(provider_id, account_id)`` — renders as a
duplicate Gemini card.

The mapping fix stops this going forward. This one-shot re-keys the already-orphaned
``default`` rows onto the canonical email so the live quota re-merges with the
email-keyed event enrichment into a single card:

  * latest_usage    -> merge each ``default`` card's *quota* into the matching email
                       card (reusing ``accumulator.merge_card_json`` so the email
                       card's ``by_model``/``msgs`` enrichment is preserved), then
                       drop the now-redundant ``default`` row. When no email card
                       exists for that ``(window_type, variant, model_id)`` grain the
                       ``default`` row is retagged in place.
  * quota_snapshots -> retag ``default`` -> email (dropping any exact-grain/ts
                       collision) so the ``%`` history / forecast is one series.
  * usage_events / usage_period_rollup -> already email-keyed; untouched.

Run with the server STOPPED (SQLite is single-writer) and APP_HOST=127.0.0.1::

  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
    python scripts/merge_gemini_default_account.py --dry-run
  # eyeball the plan, then:
  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
    python scripts/merge_gemini_default_account.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure repo root is on sys.path when the script is invoked directly.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session, select  # noqa: E402

from app.core.db import engine  # noqa: E402
from app.models.db import LatestUsage, QuotaSnapshot  # noqa: E402
from app.services.accumulator import merge_card_json  # noqa: E402

# Identity fields carried inside card_json that must NOT leak from the "default"
# card into the canonical email card during the quota merge.
_IDENTITY_KEYS = ("account_id", "account_label")


def _resolve_target_email(session: Session, provider_id: str, source: str) -> str | None:
    """The single non-source account_id present for this provider, or None."""
    accounts = {
        a
        for a in session.exec(
            select(LatestUsage.account_id).where(LatestUsage.provider_id == provider_id).distinct()
        ).all()
        if a and a != source
    }
    if len(accounts) == 1:
        return next(iter(accounts))
    return None


def _grain(row: LatestUsage | QuotaSnapshot) -> tuple:
    return (row.window_type, row.variant, row.model_id)


def _merge_latest_usage(
    session: Session, provider_id: str, source: str, target: str, apply: bool
) -> None:
    rows = session.exec(select(LatestUsage).where(LatestUsage.provider_id == provider_id)).all()
    by_account_grain: dict[str, dict[tuple, LatestUsage]] = {}
    for r in rows:
        by_account_grain.setdefault(r.account_id, {})[_grain(r)] = r

    src_rows = by_account_grain.get(source, {})
    tgt_rows = by_account_grain.get(target, {})

    print(f"latest_usage: {len(src_rows)} '{source}' card(s), {len(tgt_rows)} '{target}' card(s)")
    for grain, src in sorted(src_rows.items()):
        tgt = tgt_rows.get(grain)
        if tgt is not None:
            incoming = json.loads(src.card_json)
            for k in _IDENTITY_KEYS:
                incoming.pop(k, None)
            action = "merge quota -> email card, drop default"
        else:
            action = "retag default -> email (no email card for this grain)"
        print(f"  {grain}: {action}")
        if not apply:
            continue
        if tgt is not None:
            tgt.card_json = merge_card_json(tgt.card_json, incoming)
            if src.updated_at and (not tgt.updated_at or src.updated_at > tgt.updated_at):
                tgt.updated_at = src.updated_at
            session.delete(src)
        else:
            card = json.loads(src.card_json)
            card["account_id"] = target
            card["account_label"] = target
            src.account_id = target
            src.card_json = json.dumps(card)


def _retag_snapshots(
    session: Session, provider_id: str, source: str, target: str, apply: bool
) -> None:
    src_snaps = session.exec(
        select(QuotaSnapshot).where(
            QuotaSnapshot.provider_id == provider_id,
            QuotaSnapshot.account_id == source,
        )
    ).all()
    # Existing target grains+ts, to skip exact collisions on the unique constraint.
    existing = {
        (s.window_type, s.variant, s.model_id, s.ts)
        for s in session.exec(
            select(QuotaSnapshot).where(
                QuotaSnapshot.provider_id == provider_id,
                QuotaSnapshot.account_id == target,
            )
        ).all()
    }
    retag = collide = 0
    for s in src_snaps:
        if (s.window_type, s.variant, s.model_id, s.ts) in existing:
            collide += 1
            if apply:
                session.delete(s)
        else:
            retag += 1
            if apply:
                s.account_id = target
    print(
        f"quota_snapshots: {retag} '{source}' row(s) retag -> '{target}', "
        f"{collide} exact-ts duplicate(s) dropped"
    )


def migrate(provider_id: str, source: str, target: str | None, apply: bool) -> int:
    with Session(engine) as session:
        resolved = target or _resolve_target_email(session, provider_id, source)
        if not resolved:
            print(
                f"Could not resolve a unique target email for provider {provider_id!r} "
                f"(pass --email). Aborting."
            )
            return 1
        print(f"Folding {provider_id!r} account {source!r} -> {resolved!r}\n")

        _merge_latest_usage(session, provider_id, source, resolved, apply)
        _retag_snapshots(session, provider_id, source, resolved, apply)

        if not apply:
            print("\nDry run — no changes written. Re-run with --apply to execute.")
            return 0

        session.commit()
        print("\nApplied. The live quota now resolves under the canonical email account.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--provider", default="gemini", help="Provider id (default: gemini).")
    p.add_argument("--source", default="default", help="Account id to fold (default: default).")
    p.add_argument(
        "--email",
        default=None,
        help="Target canonical account_id. Auto-detected if exactly one non-source account exists.",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="Preview the plan, write nothing.")
    g.add_argument("--apply", action="store_true", help="Execute the migration.")
    args = p.parse_args()
    return migrate(args.provider, args.source, args.email, apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
