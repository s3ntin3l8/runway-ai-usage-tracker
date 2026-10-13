"""Native baseline paths must not weaken secret or staging failures."""

import json
from types import SimpleNamespace

import pytest

from scripts import check_secrets as cs

BASELINE = {
    "filters_used": [
        {"path": "detect_secrets.filters.common.is_baseline_file", "filename": ".secrets.baseline"}
    ],
    "results": {
        "app/file.py": [
            {
                "filename": "app/file.py",
                "hashed_secret": "fingerprint",  # pragma: allowlist secret - synthetic fingerprint fixture
                "line_number": 1,
            }
        ]
    },
}


def test_path_conversion_preserves_fingerprints_and_the_original():
    native = cs.baseline_paths(BASELINE, "\\")
    assert "app\\file.py" in native["results"]
    assert native["results"]["app\\file.py"][0]["filename"] == "app\\file.py"
    restored = cs.baseline_paths(native, "/")
    assert restored == BASELINE
    assert json.dumps(restored, sort_keys=True) == json.dumps(BASELINE, sort_keys=True)
    assert "app/file.py" in BASELINE["results"]


@pytest.mark.parametrize("code", [0, 1, 3])
def test_hook_keeps_failure_codes_and_only_saves_actual_updates(tmp_path, monkeypatch, code):
    baseline = tmp_path / "baseline.json"
    before = json.dumps(BASELINE)
    baseline.write_text(before, encoding="utf-8")
    monkeypatch.setattr(cs, "raise_exception_if_baseline_file_is_unstaged", lambda name: None)

    def scan(command, **kwargs):
        assert kwargs["check"] is False
        assert kwargs["env"]["PYTHONUTF8"] == "1"
        assert "detect_secrets.pre_commit_hook" in command
        assert str(baseline) not in command
        if code == 3:
            native = tmp_path.__class__(command[command.index("--baseline") + 1])
            updated = json.loads(native.read_text(encoding="utf-8"))
            for values in updated["results"].values():
                values[0]["line_number"] = 9
            updated["filters_used"][0]["filename"] = str(native)
            native.write_text(json.dumps(updated), encoding="utf-8")
        return SimpleNamespace(returncode=code)

    monkeypatch.setattr(cs.subprocess, "run", scan)
    assert cs.run_hook(baseline, ["app/file.py", str(baseline)]) == code
    if code == 3:
        assert (
            json.loads(baseline.read_text(encoding="utf-8"))["results"]["app/file.py"][0][
                "line_number"
            ]
            == 9
        )
    else:
        assert baseline.read_text(encoding="utf-8") == before


def test_unstaged_baseline_is_still_rejected(tmp_path, monkeypatch):
    def reject(name):
        raise ValueError("unstaged")

    monkeypatch.setattr(cs, "raise_exception_if_baseline_file_is_unstaged", reject)
    monkeypatch.setattr(cs.subprocess, "run", lambda *a, **k: pytest.fail("scan bypassed staging"))
    assert cs.run_hook(tmp_path / "baseline.json", []) == 1
