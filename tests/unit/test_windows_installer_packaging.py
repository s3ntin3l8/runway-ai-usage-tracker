"""The Windows installer must be compiled the same way everywhere.

The release build, the pull-request Windows gate and the pull-request NSIS
check each used to spell out their own ``makensis`` command line. A change to
the asset name, the version defines or the output path could therefore land
in one workflow and not in the other — the installer would be written
somewhere the release never looks for it while the pull request stayed green.

These tests pin the behaviour that replaced those command lines: one shared
helper, one canonical asset name, one *computed* output path, and a compile
that fails closed when the installer does not land where it must. They drive
the helper with a stand-in compiler, so the detection of a wrong output path
or a lost output-path override is exercised rather than asserted in prose.
"""

import json
import pathlib
import re
import shutil
import sys

import pytest

from scripts import build_windows_installer, prepare_nsis
from scripts.sidecar_pkg import asset_names

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
NSI = (ROOT / "installer" / "windows" / "runway-sidecar.nsi").read_text(encoding="utf-8")

#: Where the installer script writes when it is *not* told otherwise.
NSI_FALLBACK = re.search(r'^\s*!define OUTFILE "([^"]*)"', NSI, re.MULTILINE).group(1)

FAKE_COMPILER = """
import pathlib
import sys

args = sys.argv[1:]
out = None
for arg in args:
    if arg.startswith("-DOUTFILE="):
        out = arg.split("=", 1)[1]

script = pathlib.Path(args[-1])
if {writes!r}:
    target = pathlib.Path({writes!r})
elif out and {use_override!r}:
    target = script.parent / out.replace("\\\\", "/")
else:
    target = script.parent / {fallback!r}.replace("\\\\", "/")
target.parent.mkdir(parents=True, exist_ok=True)
target.write_bytes({payload!r})
sys.exit({exit_code})
"""


def _fake_root(tmp_path: pathlib.Path) -> pathlib.Path:
    """A repository root with just enough to resolve and compile a plan."""
    (tmp_path / "installer" / "windows").mkdir(parents=True)
    (tmp_path / "installer" / "windows" / "runway-sidecar.nsi").write_text("", encoding="utf-8")
    spec = tmp_path / "sidecar_app" / "spec"
    spec.mkdir(parents=True)
    shutil.copy(ROOT / "sidecar_app" / "spec" / "win_version.py", spec / "win_version.py")
    (tmp_path / "package.json").write_text(
        json.dumps({"version": "3.0.0-beta.10"}), encoding="utf-8"
    )
    # The installer script shows the license page from this file.
    shutil.copy(ROOT / "LICENSE", tmp_path / "LICENSE")
    return tmp_path


def _write_executable(root: pathlib.Path) -> pathlib.Path:
    """The PyInstaller output the installer ships."""
    exe = root / "dist" / "RunwaySidecar.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ executable")
    return exe


def _fake_compiler(
    tmp_path: pathlib.Path,
    *,
    writes: str | None = None,
    payload: bytes = b"MZ installer",
    exit_code: int = 0,
    use_override: bool = True,
) -> list[str]:
    """A stand-in for ``makensis`` that resolves ``OutFile`` like NSIS does."""
    script = tmp_path / "fake_makensis.py"
    script.write_text(
        FAKE_COMPILER.format(
            writes=writes,
            payload=payload,
            exit_code=exit_code,
            use_override=use_override,
            fallback=NSI_FALLBACK,
        ),
        encoding="utf-8",
    )
    return [sys.executable, str(script)]


def _fallback_output(root: pathlib.Path) -> pathlib.Path:
    """Where the installer lands when the output-path override is lost."""
    relative = NSI_FALLBACK.replace("\\", "/")
    return (root / "installer" / "windows" / relative).resolve()


# ---------------------------------------------------------------------------
# One asset name, one output path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("edge", "Runway-Sidecar-Windows-edge-setup.exe"),
        ("v2.13.0", "Runway-Sidecar-Windows-v2.13.0-setup.exe"),
        ("2.13.0", "Runway-Sidecar-Windows-v2.13.0-setup.exe"),
        ("v3.0.0-beta.1", "Runway-Sidecar-Windows-v3.0.0-beta.1-setup.exe"),
    ],
)
def test_the_installer_name_is_the_canonical_release_name(label, expected):
    assert build_windows_installer.installer_name(label) == expected
    # The self-updater's own module must agree that this is an installer.
    platform, got_label, kind = asset_names.classify(expected)
    assert platform == asset_names.WINDOWS
    assert kind == "installer"
    assert got_label == build_windows_installer.resolve_label(label)


def test_the_helper_publishes_the_name_the_release_publishes(tmp_path):
    root = _fake_root(tmp_path)
    for label in ("edge", "v2.13.0", "v3.0.0-beta.1"):
        target = build_windows_installer.plan(label, root)
        published = asset_names.installer_name(
            asset_names.WINDOWS, build_windows_installer.resolve_label(label)
        )
        assert target.installer == published
        assert target.outfile == root / "dist" / published


def test_the_output_path_is_computed_from_the_installer_script(tmp_path):
    root = _fake_root(tmp_path)
    target = build_windows_installer.plan("edge", root)
    # NSIS resolves OutFile against the .nsi directory — never the caller's cwd.
    assert target.outfile_argument == f"..\\..\\dist\\{target.installer}"
    assert target.outfile == root / "dist" / target.installer


def test_the_pr_gate_and_the_release_share_one_output_layout(tmp_path):
    root = _fake_root(tmp_path)
    edge = build_windows_installer.plan("edge", root)
    release = build_windows_installer.plan("v2.13.0", root)
    assert edge.outfile_argument.rsplit("\\", 1)[0] == release.outfile_argument.rsplit("\\", 1)[0]
    assert edge.outfile.parent == release.outfile.parent


def test_the_installers_own_default_path_is_not_the_release_path(tmp_path):
    """A lost output-path override lands on a different file — that is why
    the result is verified instead of trusted."""
    root = _fake_root(tmp_path)
    target = build_windows_installer.plan("edge", root)
    fallback = _fallback_output(root)
    assert fallback != target.outfile
    assert fallback.name != target.outfile.name


def test_the_compile_command_carries_every_define(tmp_path):
    root = _fake_root(tmp_path)
    target = build_windows_installer.plan("edge", root)
    argv = build_windows_installer.command(target)
    assert f"-DVERSION={target.version}" in argv
    assert f"-DPRODUCT_VERSION_QUAD={target.quad}" in argv
    assert f"-DOUTFILE={target.outfile_argument}" in argv
    assert argv[-1] == str(target.nsi)
    assert "-WX" not in argv
    assert "-WX" in build_windows_installer.command(target, strict=True)


# ---------------------------------------------------------------------------
# Compiling
# ---------------------------------------------------------------------------


def test_a_compile_writes_the_installer_to_the_resolved_path(tmp_path):
    root = _fake_root(tmp_path)
    _write_executable(root)
    target = build_windows_installer.plan("edge", root)
    output = build_windows_installer.compile(target, compiler=_fake_compiler(tmp_path))
    assert output == target.outfile
    assert output.read_bytes() == b"MZ installer"


def test_a_wrong_output_path_is_detected(tmp_path):
    """The installer lands elsewhere — the build must fail, not publish."""
    root = _fake_root(tmp_path)
    _write_executable(root)
    target = build_windows_installer.plan("edge", root)
    elsewhere = tmp_path / "elsewhere" / "setup.exe"
    compiler = _fake_compiler(tmp_path, writes=str(elsewhere))
    with pytest.raises(RuntimeError, match="was not written to"):
        build_windows_installer.compile(target, compiler=compiler)
    assert elsewhere.exists()
    assert not target.outfile.exists()


def test_a_stale_installer_cannot_pass_as_a_fresh_one(tmp_path):
    """A leftover file from an earlier build must not satisfy the check."""
    root = _fake_root(tmp_path)
    _write_executable(root)
    target = build_windows_installer.plan("edge", root)
    target.outfile.parent.mkdir(parents=True, exist_ok=True)
    target.outfile.write_bytes(b"MZ stale")
    compiler = _fake_compiler(tmp_path, writes=str(tmp_path / "elsewhere" / "setup.exe"))
    with pytest.raises(RuntimeError, match="was not written to"):
        build_windows_installer.compile(target, compiler=compiler)


def test_a_missing_output_override_is_detected(tmp_path):
    """Without the override the compiler uses the script's own default path."""
    root = _fake_root(tmp_path)
    _write_executable(root)
    target = build_windows_installer.plan("edge", root)
    compiler = _fake_compiler(tmp_path, use_override=False)
    with pytest.raises(RuntimeError, match="was not written to"):
        build_windows_installer.compile(target, compiler=compiler)
    assert _fallback_output(root).exists()
    assert not target.outfile.exists()


def test_a_missing_executable_stops_the_build(tmp_path):
    root = _fake_root(tmp_path)
    target = build_windows_installer.plan("edge", root)
    compiler = _fake_compiler(tmp_path)
    with pytest.raises(RuntimeError, match="is missing"):
        build_windows_installer.compile(target, compiler=compiler)
    # The compiler never ran, so no installer was produced anywhere.
    assert not target.outfile.exists()


def test_a_failed_compile_is_fatal(tmp_path):
    root = _fake_root(tmp_path)
    _write_executable(root)
    target = build_windows_installer.plan("edge", root)
    compiler = _fake_compiler(tmp_path, exit_code=2)
    with pytest.raises(RuntimeError, match="compilation failed"):
        build_windows_installer.compile(target, compiler=compiler)


def test_the_installer_must_be_a_windows_executable(tmp_path):
    root = _fake_root(tmp_path)
    _write_executable(root)
    target = build_windows_installer.plan("edge", root)
    compiler = _fake_compiler(tmp_path, payload=b"not an executable")
    with pytest.raises(RuntimeError, match="not a Windows executable"):
        build_windows_installer.compile(target, compiler=compiler)


# ---------------------------------------------------------------------------
# The workflows share the helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("workflow", ["sidecar-build.yml", "security-validation.yml", "ci-cd.yml"])
def test_the_workflows_use_the_shared_installer_helper(workflow):
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    assert "scripts.build_windows_installer" in text
    # No workflow may compile the installer on its own any more.
    assert "makensis" not in text


def test_an_edge_build_is_triggered_by_a_change_to_the_installer_helper():
    """The rolling edge build must run when the installer packaging changes."""
    edge = (WORKFLOWS / "sidecar-edge.yml").read_text(encoding="utf-8")
    assert "scripts/build_windows_installer.py" in edge


def test_the_release_build_labels_the_installer_with_its_release_label():
    build = (WORKFLOWS / "sidecar-build.yml").read_text(encoding="utf-8")
    assert '--label "${{ inputs.label }}"' in build


def test_the_pull_request_gates_build_the_edge_installer():
    gate = (WORKFLOWS / "security-validation.yml").read_text(encoding="utf-8")
    check = (WORKFLOWS / "ci-cd.yml").read_text(encoding="utf-8")
    assert "--label edge" in gate
    assert "--label edge" in check


def test_the_pull_request_windows_gate_builds_the_real_executable():
    """The gate compiles the installer from the freshly built executable."""
    gate = (WORKFLOWS / "security-validation.yml").read_text(encoding="utf-8")
    build_index = gate.index("pyinstaller --noconfirm")
    installer_index = gate.index("scripts.build_windows_installer")
    assert build_index < installer_index
    assert "dist/RunwaySidecar.exe" in gate


# ---------------------------------------------------------------------------
# The real compiler
# ---------------------------------------------------------------------------


@pytest.mark.skipif(prepare_nsis._compiler() is None, reason="NSIS is not installed")
def test_the_real_compiler_writes_the_installer_to_the_resolved_path(tmp_path):
    root = _fake_root(tmp_path)
    shutil.copy(
        ROOT / "installer" / "windows" / "runway-sidecar.nsi",
        root / "installer" / "windows" / "runway-sidecar.nsi",
    )
    shutil.copytree(ROOT / "installer" / "assets", root / "installer" / "assets")
    _write_executable(root)
    target = build_windows_installer.plan("edge", root)
    output = build_windows_installer.compile(target)
    assert output == target.outfile
    assert output.read_bytes()[:2] == b"MZ"
