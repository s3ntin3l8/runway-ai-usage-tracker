"""Compile the Windows NSIS installer under its canonical release asset name.

The release build (``.github/workflows/sidecar-build.yml``) and the
pull-request gates (``.github/workflows/security-validation.yml`` on Windows,
``ci-cd.yml``'s NSIS check on Linux) all compile the same
``installer/windows/runway-sidecar.nsi``. Each used to spell out its own
``makensis`` command line, so the asset name, the version defines and the
output path could change in one workflow and not in the other — the installer
would then be written somewhere the release never looks for it, while the pull
request gate kept passing.

This module is the single place that knows how to compile the installer:

* the file name comes from :mod:`scripts.sidecar_pkg.asset_names`, the module
  the self-updater resolves its downloads with;
* the version values come from ``sidecar_app/spec/win_version.py``;
* the output path is *computed* relative to the ``.nsi`` file (NSIS resolves
  ``OutFile`` relative to the script), never spelled out;
* the result is verified before success is reported — the installer must exist
  at the resolved path and must be a Windows executable.

Every failure is fatal: a missing executable, a missing compiler, a failed
compile or an installer that landed at the wrong path stops the build.

Usage::

    python -m scripts.build_windows_installer --label edge
    python -m scripts.build_windows_installer --label v2.13.0 --strict
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.sidecar_pkg import asset_names

ROOT = Path(__file__).resolve().parents[1]

#: The installer script, relative to the repository root.
NSI = Path("installer") / "windows" / "runway-sidecar.nsi"
#: The PyInstaller one-file build the installer ships, relative to the root.
EXE = Path("dist") / "RunwaySidecar.exe"
#: Where installers are written, relative to the repository root.
DIST = Path("dist")

#: Label used when there is no release tag (the rolling prerelease).
DEFAULT_LABEL = asset_names.EDGE_LABEL


def _win_version(root: Path) -> Any:
    """Load ``sidecar_app/spec/win_version.py`` — a script, not a package."""
    path = root / "sidecar_app" / "spec" / "win_version.py"
    spec = importlib.util.spec_from_file_location("runway_win_version", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resolve_label(label: str) -> str:
    """``"edge"`` stays as it is; a version becomes the ``v``-prefixed label."""
    if label == asset_names.EDGE_LABEL:
        return asset_names.EDGE_LABEL
    return asset_names.release_label("stable", label)


def installer_name(label: str = DEFAULT_LABEL) -> str:
    """The canonical release file name of the Windows installer for ``label``."""
    name = asset_names.installer_name(asset_names.WINDOWS, resolve_label(label))
    if not name:  # pragma: no cover - Windows always ships an installer
        raise RuntimeError(f"the {asset_names.WINDOWS} platform ships no installer")
    return name


def nsis_relative(base: Path, target: Path) -> str:
    """``target`` relative to ``base`` the way NSIS wants to read it.

    NSIS resolves ``OutFile`` against the directory of the ``.nsi`` file, so
    the path has to be relative to that directory. Backslashes are used on
    every platform — NSIS accepts them on Linux too (the installer's own
    default output path already relies on it).
    """
    return os.path.relpath(target, base).replace("/", "\\")


@dataclass(frozen=True)
class InstallerPlan:
    """Every input of one installer compile, resolved up front."""

    label: str
    installer: str
    root: Path
    nsi: Path
    exe: Path
    outfile: Path
    outfile_argument: str
    version: str
    quad: str


def _plan(label: str, root: Path) -> InstallerPlan:
    resolved = resolve_label(label)
    name = asset_names.installer_name(asset_names.WINDOWS, resolved)
    if not name:  # pragma: no cover - Windows always ships an installer
        raise RuntimeError(f"the {asset_names.WINDOWS} platform ships no installer")
    nsi = root / NSI
    outfile = root / DIST / name
    win_version = _win_version(root)
    version = win_version.read_version(root)
    return InstallerPlan(
        label=resolved,
        installer=name,
        root=root,
        nsi=nsi,
        exe=root / EXE,
        outfile=outfile,
        outfile_argument=nsis_relative(nsi.parent, outfile),
        version=version,
        quad=win_version.quad(version),
    )


def plan(label: str = DEFAULT_LABEL, root: Path = ROOT) -> InstallerPlan:
    """Resolve the compile of the installer for the release ``label``."""
    return _plan(label, root)


def plan_for_package_version(root: Path = ROOT) -> InstallerPlan:
    """Resolve the compile for the version in ``package.json`` (local builds)."""
    return _plan(_win_version(root).read_version(root), root)


def command(
    target: InstallerPlan,
    compiler: Sequence[str] = ("makensis",),
    strict: bool = False,
) -> list[str]:
    """The exact ``makensis`` invocation that compiles ``target``."""
    argv = [*compiler, "-V2"]
    if strict:
        argv.append("-WX")
    argv += [
        f"-DVERSION={target.version}",
        f"-DPRODUCT_VERSION_QUAD={target.quad}",
        f"-DOUTFILE={target.outfile_argument}",
        str(target.nsi),
    ]
    return argv


def resolve_compiler() -> list[str]:
    """The NSIS compiler, installed on demand when the host is Windows."""
    found = shutil.which("makensis")
    if found:
        return [found]
    if os.name == "nt":
        from scripts import prepare_nsis

        return [str(prepare_nsis.prepare())]
    raise RuntimeError("makensis is not installed — install NSIS to build the installer")


def verify(target: InstallerPlan) -> Path:
    """Fail closed unless the installer landed at the resolved path."""
    if not target.outfile.is_file():
        raise RuntimeError(
            f"the installer was not written to {target.outfile} — "
            "NSIS wrote it somewhere else (the output path override was lost?)"
        )
    with target.outfile.open("rb") as handle:
        header = handle.read(2)
    if header != b"MZ":
        raise RuntimeError(f"{target.outfile} is not a Windows executable")
    return target.outfile


def compile(
    target: InstallerPlan,
    compiler: Sequence[str] | None = None,
    strict: bool = False,
) -> Path:
    """Compile the installer and return the verified output path."""
    if not target.nsi.is_file():
        raise RuntimeError(f"{target.nsi} is missing")
    if not target.exe.is_file():
        raise RuntimeError(
            f"{target.exe} is missing — build it with pyinstaller sidecar_app/spec/windows.spec"
        )
    argv = command(target, compiler or resolve_compiler(), strict)
    # A leftover installer from an earlier build must not pass as a fresh one.
    target.outfile.unlink(missing_ok=True)
    print(f"Installer command: {' '.join(argv)}")
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.stderr.write(result.stdout or "")
        sys.stderr.write(result.stderr or "")
        raise RuntimeError(f"NSIS compilation failed with exit code {result.returncode}")
    return verify(target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compile the Windows installer under its canonical release name."
    )
    parser.add_argument(
        "--label",
        default=DEFAULT_LABEL,
        help="release label: 'edge' or a version such as 'v2.13.0'",
    )
    parser.add_argument(
        "--from-package-version",
        action="store_true",
        help="label the installer with the version in package.json (local builds)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat NSIS warnings as errors",
    )
    parser.add_argument("--root", default=str(ROOT), help="repository root to build in")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    target = plan_for_package_version(root) if args.from_package_version else plan(args.label, root)
    print(f"Installer label: {target.label}")
    print(f"Installer path:  {target.outfile}")
    output = compile(target, strict=args.strict)
    print(f"Installer built: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
