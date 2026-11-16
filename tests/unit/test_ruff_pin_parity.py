"""The pre-commit ruff rev and the CI ruff pin must be the same version.

``.pre-commit-config.yaml`` pins ``astral-sh/ruff-pre-commit`` and
``requirements-lint.txt`` pins ``ruff==`` for the CI lint gate. Dependabot
bumps them in two independent pull requests, so one can merge a cycle before
the other, and a local ``ruff --fix`` then rewrites code differently from the
CI gate.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
RUFF_REPO = "https://github.com/astral-sh/ruff-pre-commit"


def _pip_pin(text: str) -> str:
    match = re.search(r"^ruff==(\S+)\s*$", text, re.MULTILINE)
    assert match, "requirements-lint.txt has no `ruff==` pin"
    return match.group(1)


def _hook_rev(config: dict) -> str:
    revs = [r["rev"] for r in config["repos"] if r.get("repo") == RUFF_REPO]
    assert len(revs) == 1, f"expected one {RUFF_REPO} entry, found {len(revs)}"
    return str(revs[0]).removeprefix("v")


def test_pre_commit_ruff_rev_matches_requirements_lint_pin() -> None:
    pip = _pip_pin((ROOT / "requirements-lint.txt").read_text(encoding="utf-8"))
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hook = _hook_rev(config)
    assert hook == pip, (
        f"ruff-pre-commit rev is {hook} but requirements-lint.txt pins ruff=={pip}; "
        "bump both together so local hooks and the CI gate agree"
    )


def test_parity_helpers_detect_a_mismatch() -> None:
    config = {"repos": [{"repo": RUFF_REPO, "rev": "v0.17.4"}]}
    assert _hook_rev(config) != _pip_pin("ruff==0.17.5\n")
    assert _hook_rev(config) == _pip_pin("ruff==0.17.4\n")
