#!/usr/bin/env python3
"""Regenerate the sidecar's baked credential registry from ``app/core/registry.json``.

The sidecar ships as a single file with no access to the server's registry, so
``scripts/sidecar.py`` carries a copy between the ``INJECTED REGISTRY`` markers.
That copy is generated, never edited by hand:

    registry.json  +  scripts/sidecar_registry_overlay.json  ->  baked block

The overlay holds the *intentional* differences (each with a reason); anything
else that differs is drift, and ``tests/unit/test_sidecar_registry_generated.py``
fails on it. Run ``make sidecar-registry`` after editing either input.

Only the generated block is rewritten. An earlier generator rewrote the whole
file from a template and was abandoned once the hand edits outgrew the template.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = ROOT / "app" / "core" / "registry.json"
OVERLAY_PATH = Path(__file__).resolve().parent / "sidecar_registry_overlay.json"
SIDECAR_PATH = Path(__file__).resolve().parent / "sidecar.py"

BEGIN_MARKER = "# --- INJECTED REGISTRY ---"
END_MARKER = "# --- END INJECTED REGISTRY ---"

# The sidecar reads only these per provider; the rest of registry.json's keys
# (labels, help text, descriptions) are server UI and stay out of the binary.
_SIDECAR_PROVIDER_KEYS = ("name", "icon", "rules")


def _matches(rule: dict[str, Any], match: dict[str, Any]) -> bool:
    return all(rule.get(key) == value for key, value in match.items())


def build_registry(
    registry: dict[str, Any] | None = None, overlay: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return the sidecar registry: registry.json trimmed and overlaid.

    Raises ``ValueError`` when an overlay entry no longer matches anything, so a
    stale overlay can't pass silently.
    """
    if registry is None:
        registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    if overlay is None:
        overlay = json.loads(OVERLAY_PATH.read_text(encoding="utf-8"))

    providers: dict[str, Any] = {}
    for pid, spec in registry["providers"].items():
        providers[pid] = copy.deepcopy({k: spec[k] for k in _SIDECAR_PROVIDER_KEYS if k in spec})

    for pid, changes in overlay.get("providers", {}).items():
        if pid not in providers:
            raise ValueError(f"overlay references unknown provider {pid!r}")
        provider = providers[pid]
        for key, value in changes.get("set", {}).items():
            if provider.get(key) == value:
                raise ValueError(f"{pid}: set {key!r} is already {value!r} in registry.json")
            provider[key] = value
        for drop in changes.get("drop_rules", []):
            kept = [r for r in provider["rules"] if not _matches(r, drop["match"])]
            if len(kept) == len(provider["rules"]):
                raise ValueError(f"{pid}: drop_rules {drop['match']} matched nothing")
            provider["rules"] = kept
        for edit in changes.get("drop_mapping_keys", []):
            for key in edit["keys"]:
                hits = [
                    rule
                    for rule in provider["rules"]
                    if _matches(rule, edit["match"]) and key in rule.get("mapping", {})
                ]
                if not hits:
                    raise ValueError(f"{pid}: drop_mapping_keys {key!r} matched nothing")
                for rule in hits:
                    del rule["mapping"][key]
        for add in changes.get("add_rules", []):
            provider["rules"].append(copy.deepcopy(add["rule"]))

    for pid, add in overlay.get("add_providers", {}).items():
        if pid in providers:
            raise ValueError(f"overlay adds {pid!r}, which registry.json already defines")
        providers[pid] = copy.deepcopy(add["provider"])

    return {"providers": providers}


def _literal(value: Any, indent: int = 0) -> str:
    """Render *value* as a Python literal (not JSON: True/False/None, trailing commas).

    Every container is written one item per line with a trailing comma, which
    ``ruff format`` leaves alone, so the block is stable under the formatter.
    """
    pad = "    " * indent
    inner = "    " * (indent + 1)
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = [f"{inner}{_literal(k)}: {_literal(v, indent + 1)}," for k, v in value.items()]
        return "{\n" + "\n".join(items) + f"\n{pad}}}"
    if isinstance(value, list):
        if not value:
            return "[]"
        items = [f"{inner}{_literal(v, indent + 1)}," for v in value]
        return "[\n" + "\n".join(items) + f"\n{pad}]"
    if isinstance(value, str):
        # ensure_ascii=False keeps real characters (an emoji icon), where the old
        # hand-pasted block held lone UTF-16 surrogates that can't be encoded.
        return json.dumps(value, ensure_ascii=False)
    return repr(value)


def render_block(registry: dict[str, Any] | None = None) -> str:
    """The full text between (and including) the markers."""
    data = build_registry() if registry is None else registry
    return (
        f"{BEGIN_MARKER}\n"
        "# Generated by scripts/gen_sidecar_registry.py from app/core/registry.json and\n"
        "# scripts/sidecar_registry_overlay.json -- edit those, then `make sidecar-registry`.\n"
        f"__REGISTRY__: dict[str, Any] = {_literal(data)}\n"
        f"{END_MARKER}"
    )


_BLOCK_RE = re.compile(re.escape(BEGIN_MARKER) + r".*?" + re.escape(END_MARKER), re.DOTALL)


def apply(source: str) -> str:
    """Return *source* with the baked block replaced by a freshly rendered one."""
    if not _BLOCK_RE.search(source):
        raise ValueError(f"{BEGIN_MARKER} ... {END_MARKER} not found")
    return _BLOCK_RE.sub(lambda _m: render_block(), source, count=1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the baked block is stale")
    args = parser.parse_args(argv)
    current = SIDECAR_PATH.read_text(encoding="utf-8")
    updated = apply(current)
    if args.check:
        if updated != current:
            print("sidecar registry is stale: run `make sidecar-registry`", file=sys.stderr)
            return 1
        return 0
    if updated != current:
        SIDECAR_PATH.write_text(updated, encoding="utf-8")
        print(f"updated {SIDECAR_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
