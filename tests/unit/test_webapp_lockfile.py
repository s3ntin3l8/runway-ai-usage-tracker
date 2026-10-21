"""Unit tests for the webapp lockfile's native platform metadata.

npm copies the ``libc`` field of an optional platform package into the lockfile
verbatim. It is the only thing that tells npm which of two packages matching
the same ``os`` and ``cpu`` to install - the glibc build or the musl build.
Without it npm installs both, which grew ``node_modules/@img`` from 19M to 37M
on a glibc host.

npm 10 does not write the field at all: it is missing from npm 10's
``pkgMetaKeys``, so every lockfile npm 10 rewrites loses it. npm 11.11 added
it back, and npm 11.19 and 12.2 both reproduce this lockfile byte for byte.
The ``npm audit fix`` that regenerated the lockfile in #731 dropped it from 29
of the 35 entries, so these tests fail when a regeneration loses it.

The pin that keeps a second npm out of the tree lives in ``devEngines`` in
``webapp/package.json``; these tests cover the lockfile it protects.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEBAPP_PACKAGE_JSON = ROOT / "webapp" / "package.json"
ROOT_PACKAGE_JSON = ROOT / "package.json"
WEBAPP_LOCKFILE = ROOT / "webapp" / "package-lock.json"

# The optional platform packages whose manifest declares a libc, so npm
# records it. Every one of them ships a glibc and a musl variant under the same
# os/cpu, and npm cannot tell them apart without the field.
EXPECTED_LIBC_PACKAGES = frozenset(
    {
        "node_modules/@img/sharp-libvips-linux-arm",
        "node_modules/@img/sharp-libvips-linux-arm64",
        "node_modules/@img/sharp-libvips-linux-ppc64",
        "node_modules/@img/sharp-libvips-linux-riscv64",
        "node_modules/@img/sharp-libvips-linux-s390x",
        "node_modules/@img/sharp-libvips-linux-x64",
        "node_modules/@img/sharp-libvips-linuxmusl-arm64",
        "node_modules/@img/sharp-libvips-linuxmusl-x64",
        "node_modules/@img/sharp-linux-arm",
        "node_modules/@img/sharp-linux-arm64",
        "node_modules/@img/sharp-linux-ppc64",
        "node_modules/@img/sharp-linux-riscv64",
        "node_modules/@img/sharp-linux-s390x",
        "node_modules/@img/sharp-linux-x64",
        "node_modules/@img/sharp-linuxmusl-arm64",
        "node_modules/@img/sharp-linuxmusl-x64",
        "node_modules/@rolldown/binding-linux-arm64-gnu",
        "node_modules/@rolldown/binding-linux-arm64-musl",
        "node_modules/@rolldown/binding-linux-ppc64-gnu",
        "node_modules/@rolldown/binding-linux-s390x-gnu",
        "node_modules/@rolldown/binding-linux-x64-gnu",
        "node_modules/@rolldown/binding-linux-x64-musl",
        "node_modules/@rollup/rollup-linux-arm-gnueabihf",
        "node_modules/@rollup/rollup-linux-arm-musleabihf",
        "node_modules/@rollup/rollup-linux-arm64-gnu",
        "node_modules/@rollup/rollup-linux-arm64-musl",
        "node_modules/@rollup/rollup-linux-loong64-gnu",
        "node_modules/@rollup/rollup-linux-loong64-musl",
        "node_modules/@rollup/rollup-linux-ppc64-gnu",
        "node_modules/@rollup/rollup-linux-ppc64-musl",
        "node_modules/@rollup/rollup-linux-riscv64-gnu",
        "node_modules/@rollup/rollup-linux-riscv64-musl",
        "node_modules/@rollup/rollup-linux-s390x-gnu",
        "node_modules/@rollup/rollup-linux-x64-gnu",
        "node_modules/@rollup/rollup-linux-x64-musl",
    }
)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class TestWebappLockfileLibcMetadata:
    def test_the_lockfile_keeps_the_libc_metadata(self):
        """A regeneration that drops the field would make npm install both builds."""
        packages = _read_json(WEBAPP_LOCKFILE)["packages"]
        recorded = frozenset(name for name, meta in packages.items() if meta.get("libc"))

        missing = EXPECTED_LIBC_PACKAGES - recorded
        assert not missing, (
            "webapp/package-lock.json lost the libc metadata for "
            f"{len(missing)} package(s): {sorted(missing)}. npm needs it to pick the "
            "glibc or musl build of an optional native package. Regenerate the "
            "lockfile with the pinned npm (webapp/package.json 'packageManager'); "
            "if the dependency change is intended, update EXPECTED_LIBC_PACKAGES."
        )

    def test_no_unexpected_package_carries_the_libc_metadata(self):
        """A new entry means a dependency changed - confirm the field is still right."""
        packages = _read_json(WEBAPP_LOCKFILE)["packages"]
        recorded = frozenset(name for name, meta in packages.items() if meta.get("libc"))

        unexpected = recorded - EXPECTED_LIBC_PACKAGES
        assert not unexpected, (
            "webapp/package-lock.json gained the libc metadata for "
            f"{len(unexpected)} package(s): {sorted(unexpected)}. A dependency "
            "change usually brings new platform packages; update "
            "EXPECTED_LIBC_PACKAGES if this one is intended."
        )

    def test_every_recorded_libc_entry_declares_a_platform(self):
        """libc only means something next to an os and a cpu."""
        packages = _read_json(WEBAPP_LOCKFILE)["packages"]

        for name, meta in packages.items():
            if not meta.get("libc"):
                continue
            assert meta.get("os"), f"{name} declares libc without an os"
            assert meta.get("cpu"), f"{name} declares libc without a cpu"


class TestFrontendToolchainPin:
    def test_the_webapp_declares_the_pinned_toolchain(self):
        """npm reads devEngines from the project root before install, ci and run."""
        manifest = _read_json(WEBAPP_PACKAGE_JSON)

        assert manifest["packageManager"] == "npm@11.19.0"
        dev_engines = manifest["devEngines"]
        assert dev_engines["runtime"] == {
            "name": "node",
            "version": ">=22.22.0",
            "onFail": "error",
        }
        assert dev_engines["packageManager"] == {
            "name": "npm",
            "version": ">=11.11.0",
            "onFail": "error",
        }

    def test_the_root_manifest_declares_no_package_manager(self):
        """The root holds no lockfile, so a root package manager declaration is a trap.

        ``actions/setup-node`` v7 enables its npm cache whenever the manifest at
        the repository root names npm as the package manager, and then looks for
        a ``package-lock.json`` there. The lockfile lives in ``webapp/``, so a
        root declaration makes every workflow that sets up Node fail with
        "Dependencies lock file is not found". The pin belongs to the project
        that owns the lockfile.
        """
        root = _read_json(ROOT_PACKAGE_JSON)

        assert "packageManager" not in root
        assert "devEngines" not in root

    @pytest.mark.parametrize(
        "document",
        ["README.md", "CONTRIBUTING.md", "AGENTS.md"],
    )
    def test_the_documented_node_floor_matches_the_pin(self, document: str):
        """The docs and the enforced floor must not drift apart."""
        text = (ROOT / document).read_text(encoding="utf-8")

        assert "22.12+" not in text, (
            f"{document} still documents Node 22.12+, but react-router 8.3.0 "
            "requires 22.22.0 and the lockfile pins it."
        )
        assert "22.22+" in text
