"""The helper CDHash gate: comparison and message logic with stubbed codesign output."""

from pathlib import Path

import pytest

from scripts import check_helper_cdhash as chk

A = "a" * 40
B = "b" * 40


def out(ident: str, cdhash: str) -> str:
    return f"Executable=/x/kc_helper\nIdentifier={ident}\nFormat=Mach-O\nCDHash={cdhash}\n"


def golden_file(tmp_path: Path) -> Path:
    path = tmp_path / "golden"
    path.write_text(f"arm64 {A}\nx86_64 {B}\n")
    return path


def helper_file(tmp_path: Path) -> Path:
    path = tmp_path / "kc_helper"
    path.write_bytes(b"x")
    return path


def stub(table):
    return lambda helper, arch: table[arch]


def test_matching_hashes_pass(tmp_path):
    run = stub({"arm64": out(chk.IDENTIFIER, A), "x86_64": out(chk.IDENTIFIER, B)})
    assert chk.check(helper_file(tmp_path), golden_file(tmp_path), run) == []


def test_changed_hash_fails_with_clear_message(tmp_path):
    run = stub({"arm64": out(chk.IDENTIFIER, A), "x86_64": out(chk.IDENTIFIER, A)})
    problems = chk.check(helper_file(tmp_path), golden_file(tmp_path), run)
    assert any("x86_64" in p and A in p and B in p for p in problems)
    assert problems[-1] == chk.CHANGED
    assert "users will be asked to re-authorize" in chk.CHANGED


def test_wrong_identifier_fails(tmp_path):
    run = stub({"arm64": out("other", A), "x86_64": out(chk.IDENTIFIER, B)})
    problems = chk.check(helper_file(tmp_path), golden_file(tmp_path), run)
    assert any("identifier" in p for p in problems)


def test_unreadable_codesign_output_fails(tmp_path):
    run = stub({"arm64": "code object is not signed at all", "x86_64": out(chk.IDENTIFIER, B)})
    problems = chk.check(helper_file(tmp_path), golden_file(tmp_path), run)
    assert any("no CDHash" in p for p in problems)


def test_missing_helper_fails(tmp_path):
    problems = chk.check(tmp_path / "nope", golden_file(tmp_path), stub({}))
    assert "not found" in problems[0]


def test_parse_golden_rejects_garbage():
    with pytest.raises(ValueError):
        chk.parse_golden("arm64\n")
    with pytest.raises(ValueError):
        chk.parse_golden("# nothing\n")


def test_committed_golden_parses():
    # The app is arm64-only (PyInstaller thins to the runner's arch), so the golden
    # file must not list a slice the shipped helper can never have.
    assert set(chk.parse_golden(chk.GOLDEN.read_text())) == {"arm64"}


def test_missing_slice_names_the_architectures_present(tmp_path):
    run = stub({"arm64": out(chk.IDENTIFIER, A), "x86_64": "no architecture"})
    problems = chk.check(
        helper_file(tmp_path), golden_file(tmp_path), run, archs=lambda _h: ["arm64"]
    )
    assert "x86_64: slice missing (helper has: arm64)" in problems


def test_unreadable_slice_without_lipo_keeps_generic_message(tmp_path):
    run = stub({"arm64": out(chk.IDENTIFIER, A), "x86_64": "garbage"})
    problems = chk.check(helper_file(tmp_path), golden_file(tmp_path), run, archs=lambda _h: None)
    assert any("x86_64: no CDHash could be read" in p for p in problems)


def test_main_exit_codes(tmp_path, monkeypatch, capsys):
    helper, golden = helper_file(tmp_path), golden_file(tmp_path)
    good = {"arm64": out(chk.IDENTIFIER, A), "x86_64": out(chk.IDENTIFIER, B)}
    monkeypatch.setattr(chk, "read_codesign", stub(good))
    assert chk.main([str(helper), "--golden", str(golden)]) == 0
    bad = {"arm64": out(chk.IDENTIFIER, B), "x86_64": out(chk.IDENTIFIER, B)}
    monkeypatch.setattr(chk, "read_codesign", stub(bad))
    assert chk.main([str(helper), "--golden", str(golden)]) == 1
    assert chk.CHANGED in capsys.readouterr().err


def test_workflow_signs_helper_last_and_never_deep():
    text = (chk.ROOT / ".github/workflows/sidecar-build.yml").read_text()
    code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    assert not [ln for ln in code if "--deep" in ln]
    helper = text.index("--identifier com.runway.sidecar.keychain-helper")
    bundle = text.index('codesign --force --sign - "$app"')
    nested = text.index("codesign --force --sign - --timestamp=none")
    assert nested < helper < bundle
    assert text.count("scripts/check_helper_cdhash.py") >= 2


def test_workflows_never_deep_and_helper_not_resigned_after_bundle():
    for wf in (chk.ROOT / ".github/workflows").glob("*.yml"):
        code = [ln for ln in wf.read_text().splitlines() if not ln.lstrip().startswith("#")]
        assert not [ln for ln in code if "--deep" in ln], wf.name
    text = (chk.ROOT / ".github/workflows/sidecar-build.yml").read_text()
    after = text.split('codesign --force --sign - "$app"', 1)[1].split("- name:")[0]
    sign_cmds = [ln for ln in after.splitlines() if "codesign" in ln and "--verify" not in ln]
    assert not [ln for ln in sign_cmds if "helper" in ln]
    mk = [
        ln
        for ln in (chk.ROOT / "Makefile").read_text().splitlines()
        if not ln.lstrip().lstrip("@").startswith("#")
    ]
    assert not [ln for ln in mk if "--deep" in ln]


def test_main_reports_broken_golden_distinctly(tmp_path, capsys):
    helper = helper_file(tmp_path)
    assert chk.main([str(helper), "--golden", str(tmp_path / "missing")]) == 2
    assert "cannot check" in capsys.readouterr().err
