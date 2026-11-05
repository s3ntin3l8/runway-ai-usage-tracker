"""Where a MiniMax Code runtime store can live on this host.

Split out of ``minimax_code.py``. Discovery is pure path logic: the parser
re-exports it and calls ``_default_state_path`` through the ``minimax_code``
module, which is also what tests import.

This is not an extractor, so it stays out of ``event_extractors``: that
package is one module per provider, and each of those modules stamps a
``source_app``.
"""

from __future__ import annotations

import os
from pathlib import Path

# The environment variables the runtime itself honours for its data dir.
#
# ``join(homeDir ?? homedir(), ".minimax")`` is only the *last* step of the
# runtime's resolution: every layer above it can move the dir. ``MINIMAX_DATA_DIR``
# and ``MAVIS_DATA_DIR`` are the public knobs; the ``__MAVIS_*`` ones are
# internal and are injected by the daemon supervisor into the processes it
# spawns. The runtime's own Python helper reads ``__MAVIS_PARENT_DATA_DIR`` and
# ``MINIMAX_DATA_DIR``; its JS side reads ``MINIMAX_DATA_DIR`` and
# ``MAVIS_DATA_DIR``.
_DATA_DIR_ENV_VARS = (
    "MINIMAX_DATA_DIR",
    "MAVIS_DATA_DIR",
    "__MAVIS_RUNTIME_DATA_DIR",
    "__MAVIS_PARENT_DATA_DIR",
)

# The data-dir basenames: the current one and the legacy one it migrated
# away from. A named profile gets ``<basename>-<profile>``.
_DATA_DIR_BASENAMES = (".minimax", ".mavis")

# The desktop app's app-data dir names.
#
# The Electron profile dir is named after the app's *product* name — on
# Windows and macOS alike it is "MiniMax Agent" — while the installer and
# the CLI ship under "MiniMax Code"; both are probed, so a rename on
# either platform cannot hide the store.
_APP_DATA_DIR_NAMES = ("MiniMax Agent", "MiniMax Code", "minimax")


def _env_data_roots() -> list[Path]:
    """Return the data dirs the runtime's environment points at."""
    roots: list[Path] = []
    for name in _DATA_DIR_ENV_VARS:
        raw = os.getenv(name)
        if raw and raw.strip():
            roots.append(Path(raw.strip()).expanduser())
    return roots


def _profile_scoped_roots(home: Path) -> list[Path]:
    """Return the profile-scoped data dirs under the user's home.

    The runtime scopes its data dir per profile: ``basenameForProfile`` turns
    ``.minimax`` into ``.minimax-<profile>``. A profile comes from the desktop
    app, the CLI's ``--profile`` flag or the supervisor — and the runtime also
    derives one from the current branch when it runs inside its own
    repository, which gives each branch its own ledger.

    Globbing catches every profile without having to know how it was named.
    """
    roots: list[Path] = []
    for basename in _DATA_DIR_BASENAMES:
        try:
            matches = sorted(home.glob(f"{basename}-*"))
        except OSError:
            # An unreadable home directory simply yields no profile roots.
            continue
        roots.extend(matches)
    return roots


def _macos_app_data_roots(home: Path) -> list[Path]:
    """Return the desktop app's macOS app-data roots.

    The Electron profile lives in ``~/Library/Application Support/<name>``.
    It holds the Chromium profile and never the ledger, but it is probed as
    insurance.
    """
    base = home / "Library" / "Application Support"
    return [base / name for name in (*_APP_DATA_DIR_NAMES, "MiniMax")]


def _host_system() -> str:
    """Read ``minimax_code.platform.system`` at call time.

    Importing the extractor here would cycle (it imports this module). A
    caller that imported this module on its own gets the real platform
    instead of a KeyError.
    """
    import platform
    import sys

    for name in (
        "scripts.sidecar_pkg.event_extractors.minimax_code",
        "sidecar_pkg.event_extractors.minimax_code",
    ):
        facade = sys.modules.get(name)
        if facade is not None:
            return facade._host_system()
    return platform.system()


def _windows_app_data_root() -> Path:
    """Return the Windows roaming app-data root (``%APPDATA%``).

    Mirrors ``sidecar.get_sidecar_dir()``: on Windows it reads ``%APPDATA%``,
    so a host that redirects it — a roaming profile, a relocated ``AppData``
    — is probed where the app actually writes. The variable is a Windows one,
    so it is ignored elsewhere: an ``APPDATA`` exported on macOS or Linux
    cannot add a root the sidecar's own state dir would never use. Falls back
    to the conventional ``%USERPROFILE%\\AppData\\Roaming`` when it is unset.
    """
    if _host_system() == "Windows":
        app_data = os.getenv("APPDATA")
        if app_data and app_data.strip():
            return Path(app_data.strip())
    return Path.home() / "AppData" / "Roaming"


def _windows_app_data_roots() -> list[Path]:
    """Return the desktop app's Windows app-data roots."""
    base = _windows_app_data_root()
    return [base / name for name in _APP_DATA_DIR_NAMES]


def _data_roots() -> list[Path]:
    """Return the candidate MiniMax Code data roots, most likely first.

    The runtime resolves its data dir from ``join(homeDir ?? homedir(),
    ".minimax")``, so ``~/.minimax`` is the root on every platform — but that
    is only the *default*. The runtime also scopes the dir per profile
    (``~/.minimax-<profile>``), honours environment overrides, and used to
    live in ``~/.mavis`` before it migrated. The desktop app's macOS /
    Windows app-data roots are probed as insurance: the app's Electron
    profile is ``MiniMax Agent`` on both platforms, but that dir holds the
    Chromium profile, never the ledger.

    The list is deduplicated by *resolved* path, so the legacy ``~/.mavis``
    symlink to ``~/.minimax`` does not show up twice.
    """
    roots: list[Path] = []
    override = os.getenv("MINIMAX_CODE_HOME")
    if override:
        roots.append(Path(override).expanduser())
    roots.extend(_env_data_roots())
    home = Path.home()
    roots.append(home / ".minimax")
    roots.extend(_profile_scoped_roots(home))
    roots.append(home / ".mavis")
    roots.extend(_macos_app_data_roots(home))
    roots.extend(_windows_app_data_roots())
    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        try:
            key = str(root.resolve())
        except OSError:
            key = str(root)
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return unique


def discover_runtime_stores() -> list[Path]:
    """Return every MiniMax Code runtime store on this host.

    More than one can exist: a profile-scoped install keeps its ledger in
    ``~/.minimax-<profile>`` next to the default one, and the runtime's
    environment can point at a third. Each store carries its own cursor (they
    are keyed by database path), so all of them are read — reporting only the
    first would silently drop the usage in the others.

    The list is deduplicated by *resolved* path: on a migrated install the
    legacy ``~/.mavis`` is a symlink to ``~/.minimax``, and both would
    otherwise report the same requests twice.

    An empty list is the normal answer on a host where MiniMax Code never ran
    — the extractor then contributes nothing instead of raising.
    """
    stores: list[Path] = []
    seen: set[str] = set()
    for root in _data_roots():
        candidate = root / "v2" / "sqlite" / "runtime-state.sqlite"
        if not candidate.is_file():
            continue
        try:
            key = str(candidate.resolve())
        except OSError:
            key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        stores.append(candidate)
    return stores


def _default_state_path() -> Path:
    """Return the provider-local cursor state file path.

    Mirrors the sidecar's config dir (``~/.config/runway/sidecar`` on
    Linux/macOS, ``%APPDATA%\\runway\\sidecar`` on Windows) so the cursor
    lands next to the rest of the sidecar's state.

    This mirrors ``sidecar.get_sidecar_dir()`` instead of importing it:
    the frozen app bundles ``scripts/sidecar.py`` as *data* and runs it as
    a script, so importing it as a module here would give the frozen
    binary a second copy of the sidecar's module state. The sidecar
    passes its own ``get_sidecar_dir() / "minimax_code_watermark.json"``
    in; this is the fallback for a direct call, and a test pins the two
    together.
    """
    config_dir = os.getenv("RUNWAY_CONFIG_DIR")
    if config_dir:
        base = Path(config_dir) / "sidecar"
    elif _host_system() == "Windows":
        app_data = os.getenv("APPDATA")
        base = (
            Path(app_data) / "runway" / "sidecar"
            if app_data
            else Path.home() / "AppData" / "Roaming" / "runway" / "sidecar"
        )
    else:
        base = Path.home() / ".config" / "runway" / "sidecar"
    return base / "minimax_code_watermark.json"
