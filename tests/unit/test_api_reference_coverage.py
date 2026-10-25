"""Keep ``docs/api-reference.md`` in step with the routes and the schema.

The API reference drifted for a long time because nothing checked it: a route
was added, or a card field changed, and the prose kept describing the old
shape. These tests assert the two facts that actually matter:

* every registered route under ``/api/v1`` appears in the reference;
* the ``LimitCard`` interface example in the reference, the ``LimitCard``
  TypeScript mirror and the ``LimitCard`` Pydantic model describe the same
  card.

They are structural checks, not prose checks.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.main import app
from app.models.schemas import LimitCard

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "docs" / "api-reference.md"
TYPES = ROOT / "webapp" / "src" / "api" / "types.ts"

API_PREFIX = "/api/v1"

# The three fields `/usage/fleet` injects onto the critical card only. They are
# deliberately absent from the Pydantic model, which allows extra fields.
INJECTED_FIELDS = {"fetched_at", "next_poll_at", "cache_ttl_seconds"}


def _reference_text() -> str:
    return REFERENCE.read_text(encoding="utf-8")


def _registered_paths() -> set[str]:
    """Every route the app serves under ``/api/v1``.

    The OpenAPI schema is the source of truth here: FastAPI wraps an
    included router rather than flattening its routes onto ``app.routes``, so
    walking ``app.routes`` would find almost nothing and the check would pass
    vacuously.
    """
    schema = app.openapi()
    return {path for path in schema.get("paths", {}) if path.startswith(API_PREFIX)}


def _documented_fields() -> set[str]:
    """Top-level field names of the ``interface LimitCard`` example."""
    text = _reference_text()
    match = re.search(r"```typescript\n(.*?)```", text, re.S)
    assert match, "the LimitCard interface example is missing from the reference"
    block = match.group(1)
    start = block.find("interface LimitCard {")
    assert start != -1, "the LimitCard interface example is missing from the reference"
    fields: set[str] = set()
    for line in block[start:].splitlines()[1:]:
        if line.startswith("}") and not line.startswith(" "):
            break
        field = re.match(r"^ {2}(\w+)\??:", line)
        if field:
            fields.add(field.group(1))
    return fields


def _typescript_fields() -> set[str]:
    text = TYPES.read_text(encoding="utf-8")
    match = re.search(r"export interface LimitCard \{(.*?)\n\}", text, re.S)
    assert match, "LimitCard is missing from webapp/src/api/types.ts"
    return set(re.findall(r"^ {2}(\w+)\??:", match.group(1), re.M))


def test_every_registered_route_is_documented() -> None:
    """Every ``/api/v1`` route appears in the reference.

    The match is bounded at the end of the path. ``/api/v1/usage/events`` is a
    prefix of ``/api/v1/usage/events/range``, so a plain substring test would
    call the shorter route documented as soon as the longer one is in the
    reference - and about a dozen routes are a prefix of another.
    """
    reference = _reference_text()
    missing = sorted(
        path
        for path in _registered_paths()
        if not re.search(re.escape(path) + r"(?![\w/{-])", reference)
    )
    assert not missing, "routes missing from docs/api-reference.md:\n  " + "\n  ".join(missing)


def test_limitcard_model_fields_are_documented() -> None:
    """Every field of the Pydantic model appears in the interface example."""
    documented = _documented_fields()
    missing = sorted(set(LimitCard.model_fields) - documented)
    assert not missing, "LimitCard fields missing from the reference example:\n  " + "\n  ".join(
        missing
    )


def test_limitcard_example_matches_the_typescript_mirror() -> None:
    """The documented example and the TypeScript mirror declare the same card."""
    documented = _documented_fields()
    typescript = _typescript_fields()
    assert documented == typescript, (
        "the LimitCard example and webapp/src/api/types.ts disagree:\n"
        f"  only in the reference: {sorted(documented - typescript)}\n"
        f"  only in types.ts: {sorted(typescript - documented)}"
    )


def test_limitcard_example_adds_only_the_injected_fields() -> None:
    """The example may exceed the model only by the fleet-injected fields."""
    extra = _documented_fields() - set(LimitCard.model_fields)
    assert extra == INJECTED_FIELDS, (
        "the LimitCard example declares fields the model does not have and that "
        f"/usage/fleet does not inject: {sorted(extra - INJECTED_FIELDS)}"
    )
