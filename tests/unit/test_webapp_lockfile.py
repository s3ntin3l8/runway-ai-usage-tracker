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

The check is loss-only, deliberately. npm records the field per package and
independently - one package carrying it says nothing about another - so no
rule over the lockfile's own content can stand in for the fixture below.
Requiring an exact match instead would mean the fixture changed with every
dependency bump that moves the platform packages, and a good bump would fail
the gate the same way a stripped lockfile does.

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
#
# Keyed on the bare package name, not the path that holds it: the same package
# can sit at the top of the tree or nested under a dependency, and both copies
# have to carry the field.
#
# This is a floor, not an exact match. An entry listed here is checked for
# still carrying the field as long as it is in the lockfile; a package that
# joins the tree is not checked, and one that leaves it is skipped. Add names
# to widen the coverage - never to make a failing check pass.
EXPECTED_LIBC_PACKAGES = frozenset(
    {
        "@img/sharp-libvips-linux-arm",
        "@img/sharp-libvips-linux-arm64",
        "@img/sharp-libvips-linux-ppc64",
        "@img/sharp-libvips-linux-riscv64",
        "@img/sharp-libvips-linux-s390x",
        "@img/sharp-libvips-linux-x64",
        "@img/sharp-libvips-linuxmusl-arm64",
        "@img/sharp-libvips-linuxmusl-x64",
        "@img/sharp-linux-arm",
        "@img/sharp-linux-arm64",
        "@img/sharp-linux-ppc64",
        "@img/sharp-linux-riscv64",
        "@img/sharp-linux-s390x",
        "@img/sharp-linux-x64",
        "@img/sharp-linuxmusl-arm64",
        "@img/sharp-linuxmusl-x64",
        "@rolldown/binding-linux-arm64-gnu",
        "@rolldown/binding-linux-arm64-musl",
        "@rolldown/binding-linux-ppc64-gnu",
        "@rolldown/binding-linux-s390x-gnu",
        "@rolldown/binding-linux-x64-gnu",
        "@rolldown/binding-linux-x64-musl",
        "@rollup/rollup-linux-arm-gnueabihf",
        "@rollup/rollup-linux-arm-musleabihf",
        "@rollup/rollup-linux-arm64-gnu",
        "@rollup/rollup-linux-arm64-musl",
        "@rollup/rollup-linux-loong64-gnu",
        "@rollup/rollup-linux-loong64-musl",
        "@rollup/rollup-linux-ppc64-gnu",
        "@rollup/rollup-linux-ppc64-musl",
        "@rollup/rollup-linux-riscv64-gnu",
        "@rollup/rollup-linux-riscv64-musl",
        "@rollup/rollup-linux-s390x-gnu",
        "@rollup/rollup-linux-x64-gnu",
        "@rollup/rollup-linux-x64-musl",
    }
)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _bare_package_name(path: str) -> str:
    """The package name without the node_modules path that holds it."""
    return path.rsplit("node_modules/", 1)[-1]


def _split_policy_key(key: str) -> tuple[str, str]:
    """The package name and the optional version an allowScripts key names.

    A scoped name starts with "@", so the separator is the *second* one: in
    "@img/sharp-linux-x64" the first "@" opens the scope and the version, if
    any, follows a later "@".
    """
    separator = key.find("@", 1) if key.startswith("@") else key.find("@")
    if separator == -1:
        return key, ""
    return key[:separator], key[separator + 1 :]


def _lowest_supported_node(dev_engines_range: str) -> str:
    """The oldest Node the devEngines range accepts, as "22.22.2".

    The range this repository uses is a list of caret or ">=" arms
    ("^22.22.2 || ^24.15.0 || >=26.0.0"), and the floor is the lowest version
    any of them admits. Every arm has to be one this understands: an arm that
    is skipped instead of rejected would leave the helper confidently
    reporting a floor the range does not actually have.
    """
    versions = []
    for arm in dev_engines_range.split("||"):
        arm = arm.strip()
        for prefix in ("^", ">="):
            if arm.startswith(prefix):
                versions.append(arm[len(prefix) :].strip())
                break
        else:
            raise AssertionError(f"unsupported arm {arm!r} in {dev_engines_range!r}")
    assert versions, f"no versioned arm in {dev_engines_range!r}"
    return min(versions, key=lambda v: tuple(int(part) for part in v.split(".")))


class TestWebappLockfileLibcMetadata:
    def test_the_lockfile_keeps_the_libc_metadata(self):
        """A regeneration that drops the field would make npm install both builds.

        The check is loss-only: an entry that is still in the lockfile must
        still carry the field. A package that joins the tree is not checked
        and a package that leaves it is skipped, so a dependency bump that
        changes the platform packages does not have to touch this fixture.
        """
        packages = _read_json(WEBAPP_LOCKFILE)["packages"]

        stripped = sorted(
            path
            for path, meta in packages.items()
            if _bare_package_name(path) in EXPECTED_LIBC_PACKAGES and not meta.get("libc")
        )
        assert not stripped, (
            "webapp/package-lock.json lost the libc metadata for "
            f"{len(stripped)} package(s):\n"
            + "\n".join(f"  - {path}" for path in stripped)
            + "\nnpm needs it to pick the glibc or musl build of an optional native "
            "package. Regenerate the lockfile with the pinned npm (webapp/package.json "
            "'packageManager')."
        )

    def test_the_fixture_still_matches_the_lockfile(self):
        """A fixture that matches nothing checks nothing - that is not a pass."""
        packages = _read_json(WEBAPP_LOCKFILE)["packages"]
        present = {_bare_package_name(path) for path in packages}

        live = EXPECTED_LIBC_PACKAGES & present
        assert live, (
            "none of the packages in EXPECTED_LIBC_PACKAGES are in "
            "webapp/package-lock.json any more, so the libc check above cannot "
            "fail. Update the fixture to the platform packages the tree has now."
        )

    def test_every_recorded_libc_entry_declares_a_platform(self):
        """libc only means something next to an os and a cpu."""
        packages = _read_json(WEBAPP_LOCKFILE)["packages"]

        for name, meta in packages.items():
            if not meta.get("libc"):
                continue
            assert meta.get("os"), f"{name} declares libc without an os"
            assert meta.get("cpu"), f"{name} declares libc without a cpu"


class TestWebappInstallScriptPolicy:
    """The allowScripts policy has to describe the tree npm actually installs.

    npm's ``allowScripts`` map is a policy, not a comment. A key that matches
    nothing is a permission that grants nothing, and it reads exactly like one
    that does: this repository carried ``sharp@0.33.5`` while the tree installs
    sharp 0.35.5, which ships no install script at all. The key asserted a
    review that never happened, and the next sharp major would have moved it
    further from the truth without anything saying so.

    npm's policy is advisory by default - an uncovered install script runs
    with a notice. ``webapp/.npmrc`` makes it a hard failure instead, so a
    dependency that grows an install script breaks the install loudly. These
    tests keep the map, the lockfile and that setting in step.
    """

    def test_every_allow_scripts_key_resolves_in_the_lockfile(self):
        packages = _read_json(WEBAPP_LOCKFILE)["packages"]
        present = {_bare_package_name(path): meta.get("version") for path, meta in packages.items()}
        policy = _read_json(WEBAPP_PACKAGE_JSON).get("allowScripts", {})

        stale = []
        for key in policy:
            name, version = _split_policy_key(key)
            if name not in present:
                stale.append(f"{key} (no such package in the lockfile)")
            elif version and present[name] != version:
                stale.append(f"{key} (the lockfile has {present[name]})")
        assert not stale, (
            "webapp/package.json's allowScripts names packages the lockfile does not "
            "carry, so the policy grants nothing for them:\n"
            + "\n".join(f"  - {entry}" for entry in stale)
            + "\nA key that matches nothing is a false permission: it reads like an "
            "approved install script and approves none."
        )

    def test_every_install_script_in_the_tree_is_covered(self):
        packages = _read_json(WEBAPP_LOCKFILE)["packages"]
        policy = _read_json(WEBAPP_PACKAGE_JSON).get("allowScripts", {})
        approved = {_split_policy_key(key)[0] for key, allowed in policy.items() if allowed}

        uncovered = []
        for path, meta in packages.items():
            if not meta.get("hasInstallScript"):
                continue
            name = _bare_package_name(path)
            if name in approved:
                continue
            if meta.get("optional") and (meta.get("os") or meta.get("cpu")):
                # npm skips an optional package this platform cannot use, so its
                # install script never runs here.
                continue
            uncovered.append(f"{name}@{meta.get('version')}")
        assert not uncovered, (
            "these packages carry an install script that webapp/package.json's "
            "allowScripts does not cover:\n"
            + "\n".join(f"  - {entry}" for entry in uncovered)
            + "\nWith strict-allow-scripts in webapp/.npmrc, npm refuses to install "
            "until each one is approved or denied."
        )

    def test_the_policy_is_not_empty(self):
        policy = _read_json(WEBAPP_PACKAGE_JSON).get("allowScripts", {})

        assert policy, (
            "webapp/package.json declares no allowScripts policy, so npm has no "
            "install script approved and strict-allow-scripts rejects the install."
        )

    def test_sharp_is_installed_once_at_the_pinned_version(self):
        """The overrides entry pins one copy of sharp; the tree must hold it."""
        packages = _read_json(WEBAPP_LOCKFILE)["packages"]
        manifest = _read_json(WEBAPP_PACKAGE_JSON)
        pinned = manifest["devDependencies"]["sharp"]

        found = sorted(
            f"{_bare_package_name(path)}@{meta.get('version')}"
            for path, meta in packages.items()
            if _bare_package_name(path) == "sharp"
        )
        assert found == [f"sharp@{pinned}"], (
            f"webapp/package-lock.json installs {found}, but webapp/package.json "
            f"pins sharp {pinned} and overrides every transitive copy to it. A "
            "second copy is a second version to review."
        )

    def test_the_install_script_policy_is_enforced_rather_than_advisory(self):
        npmrc = (ROOT / "webapp" / ".npmrc").read_text(encoding="utf-8")

        assert "strict-allow-scripts=true" in npmrc, (
            "webapp/.npmrc no longer sets strict-allow-scripts=true, so npm is back "
            "to running an uncovered install script with a notice instead of "
            "failing the install."
        )


class TestFrontendToolchainPin:
    def test_the_webapp_declares_the_pinned_toolchain(self):
        """npm reads devEngines from the project root before install, ci and run."""
        manifest = _read_json(WEBAPP_PACKAGE_JSON)

        assert manifest["packageManager"] == "npm@11.19.0"
        dev_engines = manifest["devEngines"]
        assert dev_engines["runtime"] == {
            "name": "node",
            "version": "^22.22.2 || ^24.15.0 || >=26.0.0",
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
    @pytest.mark.parametrize("stale_floor", ["22.12+", "22.22+"])
    def test_the_documents_do_not_still_demand_a_stale_floor(self, document: str, stale_floor: str):
        """A stale floor is the half of the drift that silently misleads readers."""
        text = (ROOT / document).read_text(encoding="utf-8")

        assert stale_floor not in text, (
            f"{document} still documents Node {stale_floor}, but the lockfile pins a "
            "higher floor and webapp/package.json refuses to install below it."
        )

    @pytest.mark.parametrize(
        "document",
        ["README.md", "CONTRIBUTING.md", "AGENTS.md"],
    )
    def test_the_documents_demand_the_pinned_node_floor(self, document: str):
        """The documented floor has to be the floor the manifest enforces."""
        text = (ROOT / document).read_text(encoding="utf-8")
        floor = _lowest_supported_node(
            _read_json(WEBAPP_PACKAGE_JSON)["devEngines"]["runtime"]["version"]
        )

        assert f"{floor}+" in text, (
            f"{document} does not document the Node {floor}+ floor that "
            "webapp/package.json enforces through devEngines.runtime."
        )
