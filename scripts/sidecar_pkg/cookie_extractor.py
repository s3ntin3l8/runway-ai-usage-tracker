"""The sidecar's browser-cookie extractor: decryption, store tracking and reporting.

Split out of ``scripts/sidecar.py``. Behaviour, log messages and the root-logger
calls are unchanged. ``os`` and ``platform`` are read through the ``sidecar``
module at call time, as are ``__REGISTRY__`` and ``decrypt_chromium_cookie``, so
``monkeypatch.setattr(sidecar, "os" | "platform" | "decrypt_chromium_cookie", ...)``
still reaches this code. The block-tracking state lives here only; ``sidecar``
re-exports the same objects. This module must not import ``scripts.sidecar`` at
import time (circular import); the facade lookup is shared with ``offline_queue``.
"""

from __future__ import annotations

import ctypes
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts.sidecar_pkg import browser_store
from scripts.sidecar_pkg.offline_queue import _facade, _FacadeAttr
from scripts.sidecar_pkg.source_consent import cookie_request_scope

# ``os`` and ``platform`` resolve on ``sidecar`` per access (see module docstring).
os = _FacadeAttr("os")
platform = _FacadeAttr("platform")


def _sidecar_attr(name: str) -> Any:
    """Read a name back from ``sidecar`` so a test's patch of it applies."""
    return getattr(_facade(), name)


def decrypt_chromium_cookie(encrypted_value, browser_name="Chrome", host=None, schema_version=23):
    """Decrypt a Chromium-based cookie value based on the current platform."""
    if not encrypted_value:
        return None
    system = platform.system()

    # macOS decryption
    if system == "Darwin":
        from scripts.sidecar_pkg.macos_cookies import decrypt

        service = (
            "Microsoft Edge Safe Storage"
            if "Edge" in browser_name
            else f"{browser_name} Safe Storage"
        )
        return decrypt(encrypted_value, service, host, schema_version)

    # Windows decryption
    if system == "Windows":
        try:

            class DATA_BLOB(ctypes.Structure):
                _fields_ = [
                    ("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.wintypes.BYTE)),
                ]

            crypt32 = ctypes.windll.crypt32
            blob_in = DATA_BLOB()
            blob_in.cbData = len(encrypted_value)
            blob_in.pbData = ctypes.cast(encrypted_value, ctypes.POINTER(ctypes.wintypes.BYTE))
            blob_out = DATA_BLOB()
            if crypt32.CryptUnprotectData(
                ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
            ):
                buffer = ctypes.string_at(blob_out.pbData, blob_out.cbData)
                ctypes.windll.kernel32.LocalFree(blob_out.pbData)
                return buffer.decode("utf-8")
        except Exception:
            logging.debug("Windows cookie decryption failed", exc_info=True)
        return None

    # Linux decryption
    try:
        try:
            return encrypted_value.decode("utf-8")
        except Exception:
            logging.debug("Direct UTF-8 decode of Linux cookie failed, trying secretstorage")
        import hashlib

        # Try secretstorage for Chrome/Edge keys
        import secretstorage
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        conn = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(conn)
        password = None
        for item in collection.get_all_items():
            if item.get_label() in [
                "Chrome Safe Storage",
                "Chromium Safe Storage",
                "Microsoft Edge Safe Storage",
            ]:
                password = item.get_secret()
                break
        if password and (encrypted_value.startswith(b"v10") or encrypted_value.startswith(b"v11")):
            salt = b"saltysalt"
            key = hashlib.pbkdf2_hmac("sha1", password, salt, 1003, 16)
            iv = b" " * 16
            raw_ciphertext = encrypted_value[3:]
            cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
            decryptor = cipher.decryptor()
            decrypted = decryptor.update(raw_ciphertext) + decryptor.finalize()
            pad_len = decrypted[-1]
            if 1 <= pad_len <= 16:
                return decrypted[:-pad_len].decode("utf-8")
    except Exception:
        logging.debug("Linux cookie decryption failed", exc_info=True)
    return None


# Which provider wanted a cookie from a store that could not be read, so the
# cycle report can name the environment variable that would work instead and
# the server can say the source is unavailable rather than absent.
@dataclass
class _BlockedCookieStore:
    """A cookie store this cycle could not use, and who wanted a cookie from it."""

    probe: browser_store.StoreProbe
    providers: dict[str, str] = field(default_factory=dict)  # provider_id -> cookie mapping value


_cookie_store_blocked: dict[str, _BlockedCookieStore] = {}

# The reasons the server understands for an unusable store. Mirrored by
# ``app/services/blocked_sources.py``; a parity test keeps the two equal.
SOURCE_BLOCK_REASONS = browser_store.SOURCE_BLOCK_REASONS


def reset_cookie_store_tracking() -> None:
    """Forget this cycle's store probes and the providers they blocked."""
    browser_store.reset_store_probe_cache()
    _cookie_store_blocked.clear()


def _record_cookie_store_block(
    probe: browser_store.StoreProbe, provider_id: str, mapping_value: str
) -> None:
    """Note that *provider_id* wanted a cookie from a store it could not use."""
    blocked = _cookie_store_blocked.setdefault(probe.identity(), _BlockedCookieStore(probe))
    blocked.providers[provider_id] = mapping_value or ""


def _session_cookie_env_var(provider_id: str, mapping_value: str) -> str | None:
    """The environment variable that supplies the same credential as a cookie.

    The registry pairs a cookie rule with an environment rule for the providers
    whose session credential the sidecar reads from the environment (Ollama's
    ``OLLAMA_SESSION_TOKEN``, Kimi's ``KIMI_AUTH_TOKEN``). The pairing is by the
    credential field both rules map, so the lookup never offers a variable that
    supplies a different credential - and never one the sidecar does not read:
    the server-only session variables (``CLAUDE_SESSION_TOKEN``,
    ``CHATGPT_SESSION_TOKEN``) are dropped from the sidecar's registry on
    purpose, so for those providers the remedy is the paste instead.
    """
    if not mapping_value:
        return None
    rules = _sidecar_attr("__REGISTRY__").get("providers", {}).get(provider_id, {}).get("rules", [])
    for rule in rules:
        if rule.get("type") != "env":
            continue
        if (rule.get("mapping") or {}).get("value") == mapping_value:
            variable = rule.get("variable")
            if isinstance(variable, str) and variable:
                return variable
    return None


def _cookie_store_reason(state: browser_store.StoreState) -> str | None:
    """The wire reason for a store state, or ``None`` when the state is not a fault."""
    return browser_store.STATE_REASONS.get(state)


def _cookie_store_detail(state: browser_store.StoreState) -> str:
    """The sentence the server shows for a store state."""
    return browser_store.STATE_DETAILS.get(state, "the store could not be used")


def _cookie_store_remedy(providers: dict[str, str], only: str | None = None) -> str:
    """What the operator can do about a store that could not be used.

    ``only`` narrows the answer to one provider, which is what a per-provider
    report entry wants.
    """
    variables = sorted(
        {
            variable
            for provider, mapping_value in providers.items()
            if (only is None or provider == only)
            and (variable := _session_cookie_env_var(provider, mapping_value))
        }
    )
    remedy = "paste the cookie in Settings → Providers"
    if variables:
        remedy = f"set {', '.join(variables)} (or {remedy})"
    return remedy


def _cookie_store_advice(probe: browser_store.StoreProbe) -> str:
    """What the operator can do about a store that could not be used.

    Returns an empty string when the store's own detail already says it.
    """
    if probe.state is browser_store.StoreState.LOCKED:
        if probe.browser.lower() in {"chrome", "chromium", "edge"}:
            return (
                "closing the browser releases the lock but not the encryption on "
                "Chrome 127+, so it does not make these cookies readable"
            )
        return "another program is holding the store open"
    return ""


def blocked_source_entries() -> list[dict[str, str]]:
    """Manifest entries for every cookie store this cycle could not use.

    The server turns each one into an unavailable credential source, so a
    provider whose cookie sits behind a locked store is not mistaken for a
    provider nobody is signed into. The entries carry no cookie value, no
    account identifier and no filesystem path.
    """
    entries: list[dict[str, str]] = []
    for identity, blocked in sorted(_cookie_store_blocked.items()):
        reason = _cookie_store_reason(blocked.probe.state)
        if reason is None:
            continue
        detail = _cookie_store_detail(blocked.probe.state)
        for provider_id in sorted(blocked.providers):
            entries.append(
                {
                    "provider_id": provider_id,
                    "credential_origin": f"cookie:{provider_id}/{identity}",
                    "reason": reason,
                    "source_store": identity,
                    "source_detail": detail,
                    "source_remedy": _cookie_store_remedy(blocked.providers, provider_id),
                }
            )
    return entries


def report_cookie_stores() -> None:
    """Report the cookie stores this cycle could not use, one line each.

    A store that cannot be read used to raise the same
    ``sqlite3.OperationalError`` once per cookie name, for every store, on
    every cycle. One classified line per store is enough to act on and
    leaves the rest of the log readable.
    """
    unusable = browser_store.unusable_stores()
    if not unusable:
        return
    for probe in unusable:
        blocked = _cookie_store_blocked.get(probe.identity())
        providers = blocked.providers if blocked else {}
        remedy = _cookie_store_remedy(providers)
        advice = _cookie_store_advice(probe)
        reason = f"{probe.detail}; {advice}" if advice else probe.detail
        logging.warning(
            "Cookie store unavailable: %s — %s. %s.",
            probe.identity(),
            reason,
            remedy,
        )
    logging.info(
        "Cookie stores: %d of %d unusable this cycle (%s)",
        len(unusable),
        browser_store.probed_stores(),
        ", ".join(sorted({probe.state.value for probe in unusable})),
    )


class BrowserCookieExtractor:
    """Unified extractor for cookies across multiple browsers."""

    @staticmethod
    def get_all_paths():
        system = platform.system()
        home = Path.home()
        results = []

        # 1. Chromium-based
        variants = [
            {
                "name": "Chrome",
                "darwin": "Google/Chrome",
                "linux": [".config/google-chrome"],
                "win": "Google/Chrome/User Data",
            },
            {
                "name": "Chromium",
                "darwin": "Chromium",
                "linux": [".config/chromium"],
                "win": "Chromium/User Data",
            },
            {
                "name": "Edge",
                "darwin": "Microsoft Edge",
                "linux": [".config/microsoft-edge"],
                "win": "Microsoft/Edge/User Data",
            },
        ]
        for v in variants:
            dirs = []
            if system == "Darwin":
                dirs.append(home / "Library/Application Support" / v["darwin"])
            elif system == "Windows":
                la = os.getenv("LOCALAPPDATA")
                dirs.append(Path(la) / v["win"] if la else home / "AppData/Local" / v["win"])
            else:
                for lp in v["linux"]:
                    dirs.append(home / lp)

            for base in dirs:
                try:
                    if not base.exists():
                        continue
                except OSError:
                    continue
                if system == "Darwin":
                    from scripts.sidecar_pkg.macos_cookies import profiles

                    seen_stores = set()
                    for profile_path in profiles(base):
                        # Keep distinct stores; collapse aliases of the same database.
                        for store in ("Network/Cookies", "Cookies"):
                            p = profile_path / store
                            try:
                                if not p.is_file():
                                    continue
                                stat = p.stat()
                            except OSError:
                                continue
                            identity = (stat.st_dev, stat.st_ino)
                            if identity in seen_stores:
                                continue
                            seen_stores.add(identity)
                            results.append(
                                {
                                    "browser": v["name"],
                                    "type": "chromium",
                                    "path": p,
                                    "profile": profile_path.name,
                                    "store": store,
                                }
                            )
                else:
                    for profile in ["Default", "Profile 1", "Profile 2"]:
                        for rel in [profile + "/Network/Cookies", profile + "/Cookies"]:
                            p = base / rel
                            if p.exists():
                                results.append(
                                    {
                                        "browser": v["name"],
                                        "type": "chromium",
                                        "path": p,
                                        # Name the store so a failure can be
                                        # reported as "Chrome/Default/Network/Cookies"
                                        # instead of an anonymous path.
                                        "profile": profile,
                                        "store": rel[len(profile) + 1 :],
                                    }
                                )

        # 2. Linux Flatpak / Snap
        if system == "Linux":
            flatpak_bases = [
                home / ".var/app/com.google.Chrome/config/google-chrome",
                home / ".var/app/org.chromium.Chromium/config/chromium",
                home / ".var/app/com.microsoft.Edge/config/microsoft-edge",
            ]
            for base in flatpak_bases:
                if not base.exists():
                    continue
                for profile in ["Default", "Profile 1", "Profile 2"]:
                    for rel in [profile + "/Network/Cookies", profile + "/Cookies"]:
                        p = base / rel
                        if p.exists():
                            results.append(
                                {
                                    "browser": "Chromium (Flatpak)",
                                    "type": "chromium",
                                    "path": p,
                                    # Name the store so a failure can be
                                    # reported as "Chromium (Flatpak)/Default/Network/Cookies"
                                    # instead of an anonymous path.
                                    "profile": profile,
                                    "store": rel[len(profile) + 1 :],
                                }
                            )

            snap_bases = [
                home / "snap/chromium/common/chromium",
                home / "snap/firefox/common/.mozilla/firefox",
            ]
            for base in snap_bases:
                if not base.exists():
                    continue
                if "chromium" in str(base).lower():
                    for profile in ["Default", "Profile 1"]:
                        p = base / profile / "Cookies"
                        if p.exists():
                            results.append(
                                {
                                    "browser": "Chromium (Snap)",
                                    "type": "chromium",
                                    "path": p,
                                    "profile": profile,
                                    "store": "Cookies",
                                }
                            )
                elif "firefox" in str(base).lower():
                    for p in base.glob("*.default*"):
                        if (p / "cookies.sqlite").exists():
                            results.append(
                                {
                                    "browser": "Firefox (Snap)",
                                    "type": "firefox",
                                    "path": p / "cookies.sqlite",
                                    "profile": p.name,
                                    "store": "cookies.sqlite",
                                }
                            )

        # 3. Firefox
        ff_dirs = []
        if system == "Darwin":
            ff_dirs.append(home / "Library/Application Support/Firefox/Profiles")
        elif system == "Windows":
            ff_dirs.append(home / "AppData/Roaming/Mozilla/Firefox/Profiles")
        else:
            ff_dirs.append(home / ".mozilla/firefox")
        for base in ff_dirs:
            if not base.exists():
                continue
            for p in base.glob("*.default*"):
                if (p / "cookies.sqlite").exists():
                    results.append(
                        {
                            "browser": "Firefox",
                            "type": "firefox",
                            "path": p / "cookies.sqlite",
                            "profile": p.name,
                            "store": "cookies.sqlite",
                        }
                    )

        # 4. Safari (legacy, sandbox container and separate profile stores).
        if system == "Darwin":
            from scripts.sidecar_pkg.safari_cookies import stores

            results.extend(stores(home))

        return results

    @staticmethod
    @cookie_request_scope
    def get_macos_candidates(provider, domains, name, *, scan_outcomes=None):
        """Return independent sources; optional scan_outcomes is an internal diagnostic sink.

        Production emits the same sanitized outcomes to logs, not ingestion manifests.
        """
        from scripts.sidecar_pkg.cookie_scan import report_scan, report_scan_summary
        from scripts.sidecar_pkg.identity import credential_fingerprint, keyed_credential_origin
        from scripts.sidecar_pkg.macos_cookies import scan_cookies

        scan_outcomes = scan_outcomes if scan_outcomes is not None else []
        first_outcome = len(scan_outcomes)
        candidates = []
        allowed_domains = (
            {"claude.ai"}
            if provider == "anthropic"
            else {"platform.claude.com", "console.anthropic.com"}
            if provider == "anthropic_console"
            else {domain.lstrip(".") for domain in domains}
        )
        domains = [domain for domain in domains if domain.lstrip(".") in allowed_domains]
        if provider == "anthropic" and os.getenv(
            "RUNWAY_CLAUDE_DESKTOP_CREDENTIALS", "1"
        ).strip().lower() not in {"0", "false", "off", "no"}:
            from scripts.sidecar_pkg.claude_desktop import scan_session_cookie

            base_origin = f"cookie:{provider}/Claude Desktop/Default/Cookies"
            scanned = scan_session_cookie(
                Path.home() / "Library/Application Support/Claude/Cookies"
            )
            report_scan("Claude Desktop", base_origin, scanned, scan_outcomes)
            for value in scanned.values:
                origin = keyed_credential_origin(base_origin, credential_fingerprint(value))
                candidates.append((value, origin))
        for target in BrowserCookieExtractor.get_all_paths():
            browser = target["browser"]
            service = (
                "Microsoft Edge Safe Storage" if "Edge" in browser else f"{browser} Safe Storage"
            )
            origin = f"cookie:{provider}/{browser}/{target.get('profile', target['path'].parent.name)}/{target.get('store', target['path'].name)}"
            for domain in domains:
                bare = domain.lstrip(".")
                values = []
                try:
                    if target["type"] == "chromium":
                        scanned = scan_cookies(target["path"], service, bare, name)
                        values = scanned.values
                        report_scan(browser, origin, scanned, scan_outcomes)
                    elif target["type"] == "safari":
                        from scripts.sidecar_pkg.safari_cookies import scan_cookies as scan_safari

                        if target.get("discovery_status") == "inaccessible":
                            from scripts.sidecar_pkg.cookie_scan import StoreScan

                            scanned = StoreScan("inaccessible")
                        else:
                            scanned = scan_safari(target["path"], bare, name)
                        report_scan(browser, origin, scanned, scan_outcomes)
                        values = [cookie["value"] for cookie in scanned.values]
                    elif target["type"] == "firefox":
                        from contextlib import closing

                        with closing(
                            sqlite3.connect(
                                target["path"].resolve().as_uri() + "?mode=ro", uri=True
                            )
                        ) as conn:
                            values = [
                                row[0]
                                for row in conn.execute(
                                    "SELECT value FROM moz_cookies WHERE name = ? AND (host = ? OR host = ? OR host LIKE ?)",
                                    (name, bare, "." + bare, "%." + bare),
                                )
                            ]
                except (OSError, sqlite3.Error, ValueError, TypeError):
                    continue
                for value in values:
                    if not value:
                        continue
                    candidate = (
                        value,
                        keyed_credential_origin(origin, credential_fingerprint(value))
                        if provider in {"anthropic", "anthropic_console"}
                        else origin,
                    )
                    if candidate not in candidates:
                        candidates.append(candidate)
        report_scan_summary(scan_outcomes[first_outcome:])
        return candidates

    @staticmethod
    def parse_safari(path):
        """Compatibility wrapper; malformed/inaccessible stores yield no cookies."""
        from scripts.sidecar_pkg.safari_cookies import scan_cookies

        return scan_cookies(path).values

    @staticmethod
    @cookie_request_scope
    def get_cookie(domain, name, provider=None, mapping_value=None):
        # Normalise the domain to its bare form (no leading dot) so we can
        # express a precise suffix match. The previous LIKE %{domain}% was
        # a substring match — "anthropic.com" matched "evil-anthropic.com"
        # and "anthropic.com.evil.org" alike, which is wrong both for
        # security and for correctness if a malicious cookie row ever
        # landed in the user's browser store.
        bare = (domain or "").lstrip(".")
        dotted = "." + bare
        like_subdomain = "%." + bare

        # Desktop uses its own Electron store and Keychain service. Keep this
        # scoped to Claude's session cookie; other provider rules never read it.
        if (
            platform.system() == "Darwin"
            and bare == "claude.ai"
            and name == "sessionKey"
            and os.getenv("RUNWAY_CLAUDE_DESKTOP_CREDENTIALS", "1").strip().lower()
            not in {"0", "false", "off", "no"}
        ):
            try:
                from scripts.sidecar_pkg.claude_desktop import scan_session_cookie
                from scripts.sidecar_pkg.cookie_scan import report_scan, report_scan_summary

                scanned = scan_session_cookie(
                    Path.home() / "Library/Application Support/Claude/Cookies"
                )
                outcomes = []
                report_scan(
                    "Claude Desktop",
                    "cookie:anthropic/Claude Desktop/Default/Cookies",
                    scanned,
                    outcomes,
                )
                report_scan_summary(outcomes)
                desktop_cookie = next(iter(scanned.values), None)
                if desktop_cookie:
                    logging.info("Claude session cookie source: Claude Desktop")
                    return desktop_cookie
            except ImportError:
                logging.warning(
                    "Claude Desktop credential helper unavailable; trying browser sources"
                )

        for target in BrowserCookieExtractor.get_all_paths():
            # Safari's store is not a SQLite database and macOS keeps its own
            # decryption path, so neither is probed. Only the SQLite stores
            # (Chromium and Firefox outside macOS) are classified, and they
            # are classified once per cycle rather than once per cookie name.
            if target["type"] == "safari" or (
                target["type"] == "chromium" and platform.system() == "Darwin"
            ):
                try:
                    if target["type"] == "safari":
                        for c in BrowserCookieExtractor.parse_safari(target["path"]):
                            host = (c.get("domain") or "").lower()
                            if c["name"] == name and (
                                host == bare or host == dotted or host.endswith(dotted)
                            ):
                                logging.info(
                                    "Cookie source: %s", target.get("browser", target["type"])
                                )
                                return c["value"]
                    else:
                        from scripts.sidecar_pkg.macos_cookies import read_cookies

                        browser = target["browser"]
                        service = (
                            "Microsoft Edge Safe Storage"
                            if "Edge" in browser
                            else f"{browser} Safe Storage"
                        )
                        values = read_cookies(target["path"], service, bare, name)
                        if values:
                            return values[0]
                except Exception:
                    logging.warning(
                        "Cookie extraction failed for browser target (%s, name=%s)",
                        target.get("browser"),
                        name,
                        exc_info=True,
                    )
                continue

            try:
                probe = browser_store.probe_store(target)
            except Exception:
                # A store that cannot even be classified must not break the
                # provider that was asking for it.
                logging.debug("Cookie store probe failed", exc_info=True)
                continue
            if not probe.readable:
                # A store that cannot be read is skipped for the rest of the
                # cycle. The reason is reported once per cycle by
                # ``report_cookie_stores`` instead of once per cookie name.
                if provider:
                    _record_cookie_store_block(probe, provider, mapping_value or "")
                continue
            try:
                conn = browser_store.open_store(probe)
                if conn is None:
                    continue
                with closing(conn):
                    cursor = conn.cursor()
                    if target["type"] == "chromium":
                        cursor.execute(
                            "SELECT encrypted_value FROM cookies "
                            "WHERE name = ? "
                            "AND (host_key = ? OR host_key = ? OR host_key LIKE ?)",
                            (name, bare, dotted, like_subdomain),
                        )
                        row = cursor.fetchone()
                        if row:
                            val = _sidecar_attr("decrypt_chromium_cookie")(
                                row[0], target["browser"]
                            )
                            if val:
                                logging.info(
                                    "Cookie source: %s", target.get("browser", target["type"])
                                )
                                return val
                            # The store opened but the value could not be
                            # decrypted. An app-bound store is readable as a
                            # file, so the probe does not skip it and this is
                            # the only place the block becomes visible.
                            if provider and browser_store.is_app_bound(row[0]):
                                # The probe samples one row, and Chrome 127+
                                # keeps the cookies written before the
                                # upgrade as v10 while writing v20 only for
                                # the ones set afterwards. The sample can
                                # therefore call a store readable whose
                                # provider cookie is app-bound; the value
                                # that failed to decrypt is the better
                                # evidence.
                                #
                                # The record is gated on that value too, not
                                # on the store: reclassifying the probe
                                # sticks for the rest of the cycle, and a
                                # value that failed for some other reason
                                # would then be labelled app-bound when it
                                # is not. The reclassification still buys
                                # the store its line in the log.
                                probe = browser_store.mark_app_bound(probe)
                                _record_cookie_store_block(probe, provider, mapping_value or "")
                    else:  # Firefox
                        cursor.execute(
                            "SELECT value FROM moz_cookies "
                            "WHERE name = ? "
                            "AND (host = ? OR host = ? OR host LIKE ?)",
                            (name, bare, dotted, like_subdomain),
                        )
                        row = cursor.fetchone()
                        if row:
                            logging.info("Cookie source: %s", target.get("browser", target["type"]))
                            return row[0]
            except Exception:
                # The store opened, so this is a schema or decoding problem
                # rather than an access problem: it is per-cookie detail and
                # belongs at debug level.
                logging.debug(
                    "Cookie extraction failed for browser target (%s, name=%s)",
                    target.get("browser"),
                    name,
                    exc_info=True,
                )
                continue
        return None
