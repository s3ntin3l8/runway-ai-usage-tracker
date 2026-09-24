"""Canonical sidecar release asset names — the single source of truth.

Every name a release (stable ``vX.Y.Z`` or the rolling ``edge`` prerelease)
publishes is derived here. The self-updater resolves its download through this
module, and ``tests/unit/test_sidecar_release_contract.py`` asserts the build
workflow (``.github/workflows/sidecar-build.yml``) emits exactly these names, so
the two can never drift apart again (they did: stable builds were published as
``…-v2.12.0.zip`` while the updater looked for ``…-2.12.0.zip``).

Two kinds of asset per desktop platform:

* **installer** — what people download: a drag-to-Applications ``.dmg`` on
  macOS, a per-user NSIS ``-setup.exe`` on Windows. The updater never fetches
  these.
* **payload** — the portable archive (``.zip`` / ``.tar.gz``) the self-updater
  downloads and swaps in place. Linux ships payloads only.

Every asset has a sibling ``<name>.sha256``; the release also carries an
unversioned ``SHA256SUMS.txt`` plus Sigstore ``.sig`` / ``.cert`` files.

Stdlib only, no ``app.*`` imports — this ships inside the frozen sidecar.
"""

from __future__ import annotations

import re

PREFIX = "Runway-Sidecar"
EDGE_LABEL = "edge"
CHECKSUMS_FILE = "SHA256SUMS.txt"

# Release platform keys, in publish order.
MACOS = "macOS"
WINDOWS = "Windows"
LINUX = "Linux"
LINUX_CLI = "Linux-CLI"
PLATFORMS: tuple[str, ...] = (MACOS, WINDOWS, LINUX, LINUX_CLI)

_PAYLOAD_EXT = {MACOS: "zip", WINDOWS: "zip", LINUX: "tar.gz", LINUX_CLI: "tar.gz"}


def release_label(channel: str, version: str | None = None) -> str:
    """``"edge"`` for the edge channel, else the ``v``-prefixed release tag.

    Accepts the version with or without a leading ``v`` (tags are ``vX.Y.Z``).
    """
    if channel == "edge":
        return EDGE_LABEL
    ver = (version or "").strip().lstrip("vV")
    if not ver:
        raise ValueError("stable asset names need a version")
    return f"v{ver}"


def platform_key(sys_platform: str, target: str = "tray") -> str | None:
    """Map ``sys.platform`` + target (``"tray"``/``"cli"``) to a release platform key."""
    if sys_platform == "darwin":
        return MACOS
    if sys_platform == "win32":
        return WINDOWS
    if sys_platform.startswith("linux"):
        return LINUX_CLI if target == "cli" else LINUX
    return None


def payload_name(platform: str, label: str) -> str:
    """The self-update archive, e.g. ``Runway-Sidecar-macOS-v2.13.0.zip``."""
    return f"{PREFIX}-{platform}-{label}.{_PAYLOAD_EXT[platform]}"


def legacy_payload_name(platform: str, label: str) -> str | None:
    """Pre-contract name the updater used to look for (``v`` stripped).

    Never published by CI; kept only as a lookup fallback so a hand-uploaded
    release in the old shape still resolves. ``None`` for edge (unchanged).
    """
    if label == EDGE_LABEL:
        return None
    return f"{PREFIX}-{platform}-{label.lstrip('v')}.{_PAYLOAD_EXT[platform]}"


def installer_name(platform: str, label: str) -> str | None:
    """The user-facing installer, or ``None`` where the platform ships none."""
    if platform == MACOS:
        return f"{PREFIX}-{MACOS}-{label}.dmg"
    if platform == WINDOWS:
        return f"{PREFIX}-{WINDOWS}-{label}-setup.exe"
    return None


def release_assets(label: str) -> list[str]:
    """Every primary asset a release publishes (no ``.sha256``/``.sig`` siblings)."""
    names: list[str] = []
    for plat in PLATFORMS:
        inst = installer_name(plat, label)
        if inst:
            names.append(inst)
        names.append(payload_name(plat, label))
    return names


_ASSET_RE = re.compile(
    rf"^{PREFIX}-(?P<platform>macOS|Windows|Linux-CLI|Linux)-(?P<label>edge|v[0-9][^-]*?(?:-[0-9A-Za-z.]+)?)"
    r"(?P<suffix>\.dmg|-setup\.exe|\.zip|\.tar\.gz)$"
)


def classify(name: str) -> tuple[str, str, str] | None:
    """Parse an asset name into ``(platform, label, kind)``; kind is installer|payload.

    Returns ``None`` for anything that isn't a primary sidecar asset (checksums,
    signatures, unrelated files).
    """
    m = _ASSET_RE.match(name)
    if not m:
        return None
    kind = "installer" if m["suffix"] in (".dmg", "-setup.exe") else "payload"
    return m["platform"], m["label"], kind


def main(argv: list[str] | None = None) -> int:
    """Print ``key=value`` lines for a CI step's ``$GITHUB_OUTPUT``.

    ``python -m scripts.sidecar_pkg.asset_names <platform> <label>`` →
    ``payload=…`` and, where the platform has one, ``installer=…``. The build
    workflow asks this module for every file name instead of spelling them
    out, so the published names and the updater's lookups cannot drift.
    """
    import sys

    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] not in PLATFORMS:
        print(f"usage: asset_names <{'|'.join(PLATFORMS)}> <vX.Y.Z|edge>", file=sys.stderr)
        return 2
    plat, label = args
    if label != EDGE_LABEL:
        label = release_label("stable", label)
    print(f"payload={payload_name(plat, label)}")
    inst = installer_name(plat, label)
    if inst:
        print(f"installer={inst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
