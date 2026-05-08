# app/services/accumulator.py
# ruff: noqa: F821  # Phase 1 schema reset: process_delta body references deleted CumulativeUsage; rewritten in Phase 3
import json
from datetime import UTC, datetime

from sqlmodel import Session, select

# CumulativeUsage removed in event-sourced schema reset (Phase 1)
from app.services.account_identity import resolve_account_id


def _join_distinct(a: str | None, b: str | None) -> str | None:
    if a == b or b is None:
        return a
    parts = dict.fromkeys(p for s in (a, b) for p in (s or "").split(",") if p)
    return ",".join(parts) or None


# Quota fields that must stay unit-consistent — don't let a token-unit enrichment
# source overwrite percentage-unit quota data from the web collector, or vice versa.
_QUOTA_FIELDS = frozenset({"used_value", "limit_value", "pct_used", "unit_type", "currency"})


def merge_card_json(existing: str | None, incoming: dict) -> str:
    """Merge an incoming card payload into an existing row's JSON; pass partial dicts, not full model_dump()."""
    if not existing:
        return json.dumps(incoming)

    existing_dict = json.loads(existing)
    merged = {**existing_dict}

    existing_unit = existing_dict.get("unit_type")
    incoming_unit = incoming.get("unit_type")
    # When units conflict (e.g. local enrichment sends tokens into a percent-based
    # quota row) protect the quota fields so the existing quota data is preserved.
    unit_mismatch = bool(existing_unit and incoming_unit and existing_unit != incoming_unit)

    for key, value in incoming.items():
        if key == "by_model":
            # {} means "not populated by this source" — legitimate empty resets are unrepresentable
            if isinstance(value, dict) and value:
                merged[key] = value
        elif key in ("data_source", "input_source"):
            merged[key] = _join_distinct(existing_dict.get(key), value)
        elif value is not None:
            if unit_mismatch and key in _QUOTA_FIELDS:
                continue
            merged[key] = value

    return json.dumps(merged)


class UsageAccumulator:
    def __init__(self, session: Session):
        self.session = session

    def process_delta(
        self,
        provider_id: str,
        account_id: str,
        sidecar_id: str,
        unit_type: str,
        delta_value: float,
        timestamp: str,
        account_label: str | None = None,
    ) -> None:
        if delta_value <= 0:
            return

        canonical_account_id = resolve_account_id(provider_id, account_id, account_label)

        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        year_key = dt.strftime("%Y")
        month_key = dt.strftime("%Y-%m")

        periods = [("lifetime", "all"), ("year", year_key), ("month", month_key)]

        for p_type, p_key in periods:
            stmt = select(CumulativeUsage).where(
                CumulativeUsage.provider_id == provider_id,
                CumulativeUsage.account_id == canonical_account_id,
                CumulativeUsage.period_type == p_type,
                CumulativeUsage.period_key == p_key,
                CumulativeUsage.unit_type == unit_type,
            )
            record = self.session.exec(stmt).first()

            if not record:
                record = CumulativeUsage(
                    provider_id=provider_id,
                    account_id=canonical_account_id,
                    sidecar_id=sidecar_id,
                    period_type=p_type,
                    period_key=p_key,
                    unit_type=unit_type,
                    total_value=0.0,
                )
                self.session.add(record)

            record.total_value += delta_value
            record.last_updated = datetime.now(UTC)

        self.session.commit()
