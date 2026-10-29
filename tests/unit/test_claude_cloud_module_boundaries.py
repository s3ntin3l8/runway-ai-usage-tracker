"""The Claude cloud cluster may not reach into another module's private members.

The cluster's shared pieces live in the public support module. A cross-module
read of an underscore-prefixed member is the coupling this refactor removed, so
this guard fails if one reappears.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLUSTER = sorted(
    [
        *ROOT.glob("app/services/claude_cloud_*.py"),
        ROOT / "app/services/claude_chat_fixture.py",
        *ROOT.glob("app/api/endpoints/claude_cloud_*.py"),
    ]
)

# Another module's private member, read through the module alias it is imported as.
PRIVATE_ATTRIBUTE = re.compile(r"\b(?:probe|authority|support)\._[A-Za-z]\w*")
# A private member imported by name from another cluster module.
PRIVATE_IMPORT = re.compile(
    r"^from\s+app\.services\.claude_(?:cloud_\w+|chat_fixture)\s+import\s+([^\n(]+)$",
    re.MULTILINE,
)


def imported_names(statement: str) -> list[str]:
    return [name.strip().split(" as ")[0] for name in statement.split(",")]


@pytest.mark.parametrize("path", CLUSTER, ids=lambda path: path.name)
def test_the_cluster_reads_no_private_member_of_another_module(path: Path):
    source = path.read_text()
    assert not PRIVATE_ATTRIBUTE.search(source), (
        f"{path.relative_to(ROOT)} reads another module's private member"
    )
    for statement in PRIVATE_IMPORT.findall(source):
        private = [name for name in imported_names(statement) if name.startswith("_")]
        assert not private, f"{path.relative_to(ROOT)} imports the private {private}"


def test_the_cluster_is_not_empty():
    assert len(CLUSTER) >= 5
