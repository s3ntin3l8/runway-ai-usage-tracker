"""Keep ``docs/collectors/`` honest about what it documents.

Two provider docs in this directory pointed at collector modules that had been
deleted months earlier, and one of them described an endpoint that never
existed. Nothing caught either, because no test looked.

These checks are structural:

* every registry provider has its own row in the index, and a document behind
  that row;
* every collector document is reachable from the index;
* every source path a doc names actually exists in the tree.

The provider check reads the index **row by row**. Concatenating every
document into one blob and searching it would not work: a provider named in
another provider's document would pass as documented, and one provider id that
is a prefix of another (``mini`` inside ``minimax``) would pass as a
substring.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COLLECTORS = ROOT / "docs" / "collectors"
REGISTRY = ROOT / "app" / "core" / "registry.json"

# A repo-relative source path inside backticks, e.g. `app/services/collectors/x.py`.
PATH_RE = re.compile(
    r"`((?:app|scripts|sidecar_app|webapp|tests)/[A-Za-z0-9_./-]+\.(?:py|ts|tsx|json|md))`"
)
# The `**File:**` front-matter line.
FILE_RE = re.compile(r"^\*\*File:\*\*\s*`([^`]+)`", re.MULTILINE)
# A table row whose first cell is a backticked id: `| `anthropic` | ... |`.
ROW_RE = re.compile(r"^\|\s*`([^`]+)`\s*\|(.*)$")


def _provider_ids() -> list[str]:
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    return sorted(data["providers"].keys())


def _doc_files() -> list[Path]:
    return sorted(path for path in COLLECTORS.glob("*.md") if path.name != "README.md")


def _index_text() -> str:
    index = COLLECTORS / "README.md"
    assert index.exists(), "docs/collectors/README.md is missing"
    return index.read_text(encoding="utf-8")


def _index_rows(text: str) -> dict[str, str]:
    """Every id that heads a table row -> the rest of that row."""
    rows: dict[str, str] = {}
    for line in text.splitlines():
        match = ROW_RE.match(line)
        if match:
            rows.setdefault(match.group(1), match.group(2))
    return rows


def _link_targets(row: str) -> list[str]:
    """Every link target on a table row, with any ``#anchor`` stripped."""
    return [target.split("#", 1)[0] for target in re.findall(r"\]\(([^)]+)\)", row)]


def test_every_provider_has_a_collector_doc() -> None:
    """Each registry provider has its own row, and a document behind it."""
    rows = _index_rows(_index_text())
    missing = [provider for provider in _provider_ids() if provider not in rows]
    assert not missing, "registry providers with no row in the collector index: " + ", ".join(
        missing
    )

    undocumented: list[str] = []
    for provider in _provider_ids():
        links = _link_targets(rows[provider])
        # A pure in-page anchor (`#provider-map`) resolves to the directory
        # itself, so it is not a link to a document.
        if not any(link and (COLLECTORS / link).exists() for link in links):
            undocumented.append(provider)
    assert not undocumented, (
        "providers whose index row links to no existing document: " + ", ".join(undocumented)
    )


def test_every_collector_doc_is_indexed() -> None:
    """No collector document is orphaned from the index."""
    text = _index_text()
    orphans = [
        path.name
        for path in _doc_files()
        if not re.search(r"\]\(" + re.escape(path.name) + r"(?:#[^)]*)?\)", text)
    ]
    assert not orphans, "collector documents the index does not link: " + ", ".join(orphans)


def test_collector_docs_exist() -> None:
    """There is at least one provider doc, and every one names its module."""
    docs = _doc_files()
    assert docs, "docs/collectors/ has no provider documentation"

    missing_front_matter: list[str] = []
    for path in docs:
        text = path.read_text(encoding="utf-8")
        match = FILE_RE.search(text)
        if not match:
            missing_front_matter.append(path.name)
            continue
        target = match.group(1).strip()
        if not (ROOT / target).exists():
            missing_front_matter.append(f"{path.name} -> {target}")
    assert not missing_front_matter, (
        "collector docs without a valid `**File:**` front-matter:\n  "
        + "\n  ".join(missing_front_matter)
    )


def test_paths_named_in_collector_docs_exist() -> None:
    """Every source path a collector doc names is a real file."""
    broken: list[str] = []
    for path in _doc_files():
        text = path.read_text(encoding="utf-8")
        for match in PATH_RE.finditer(text):
            target = match.group(1)
            if not (ROOT / target).exists():
                broken.append(f"{path.name} -> {target}")
    assert not broken, "collector docs name files that do not exist:\n  " + "\n  ".join(broken)


def test_collector_index_exists() -> None:
    """The directory has an index mapping providers to their modules."""
    text = _index_text()
    rows = _index_rows(text)
    missing = [provider for provider in _provider_ids() if provider not in rows]
    assert not missing, "providers missing from the collector index: " + ", ".join(missing)
