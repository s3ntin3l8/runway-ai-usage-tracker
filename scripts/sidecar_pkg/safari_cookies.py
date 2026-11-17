"""Read-only Safari binary-cookie discovery and bounded structural decoding."""

import errno
import json
import logging
import math
import os
import platform
import struct
import tempfile
import time
from pathlib import Path
from typing import Any

from scripts.sidecar_pkg.cookie_scan import StoreScan

_APPLE_EPOCH = 978307200
_MAX_STORE_BYTES = 64 * 1024 * 1024
_ACCESS_DENIED_ERRNOS = frozenset({errno.EPERM, errno.EACCES})
_FULL_DISK_ACCESS_WARNING = (
    "Safari cookies cannot be read: Full Disk Access is needed for Runway Sidecar "
    "(System Settings -> Privacy & Security -> Full Disk Access)"
)
_logger = logging.getLogger(__name__)
_probe_log_state: dict[str, str | None] = {"last": None}


def _sidecar_config_path() -> Path:
    """Return the sidecar config.json path without importing scripts.sidecar."""
    override = os.getenv("RUNWAY_CONFIG_DIR")
    if override:
        base = Path(override) / "sidecar"
    elif platform.system() == "Windows":
        app_data = os.getenv("APPDATA")
        base = (
            Path(app_data) / "runway" / "sidecar"
            if app_data
            else Path.home() / "AppData" / "Roaming" / "runway" / "sidecar"
        )
    else:
        base = Path.home() / ".config" / "runway" / "sidecar"
    return base / "config.json"


def _persist_last_state(
    state: str,
    config: dict[str, Any] | None = None,
    *,
    dismissed: str | None = None,
    preserve_rearmed: bool = False,
) -> None:
    """Persist the latest Safari access state in config when it changes."""
    path = _sidecar_config_path()
    try:
        current: dict[str, Any] = {}
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = loaded
        prev_state = current.get("safari_fda_last_state")
        if not isinstance(prev_state, str) and config is not None:
            cfg_last = config.get("safari_fda_last_state")
            if isinstance(cfg_last, str):
                prev_state = cfg_last
        cur_dismissed = (
            config.get("safari_fda_notice_dismissed_state") if config is not None else None
        )
        if not isinstance(cur_dismissed, str):
            disk_dismissed = current.get("safari_fda_notice_dismissed_state")
            if isinstance(disk_dismissed, str):
                cur_dismissed = disk_dismissed
        else:
            current["safari_fda_notice_dismissed_state"] = cur_dismissed
        changed = prev_state != state or current.get("safari_fda_last_state") != state
        current["safari_fda_last_state"] = state
        if dismissed is not None:
            if current.get("safari_fda_notice_dismissed_state") != dismissed:
                current["safari_fda_notice_dismissed_state"] = dismissed
                changed = True
            if not preserve_rearmed and current.pop("safari_fda_notice_rearmed", None) is not None:
                changed = True
        elif isinstance(prev_state, str) and prev_state != state:
            if cur_dismissed == "full_disk_access_missing":
                current["safari_fda_notice_rearmed"] = True
            if cur_dismissed == "full_disk_access_missing" and state != "full_disk_access_missing":
                current["safari_fda_notice_dismissed_state"] = ""
            changed = True
        if config is not None:
            config["safari_fda_last_state"] = state
            if isinstance(current.get("safari_fda_notice_dismissed_state"), str):
                config["safari_fda_notice_dismissed_state"] = current[
                    "safari_fda_notice_dismissed_state"
                ]
        if not changed:
            return
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".config-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(current, output, indent=2)
                output.flush()
                os.fsync(output.fileno())
            if os.name == "posix":
                os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                # Temporary file was already moved into place by os.replace.
                pass
    except (OSError, ValueError):
        # State persistence is best-effort; an unreadable or unwritable config must not break probing.
        pass


def _is_access_denied(exc: OSError) -> bool:
    return isinstance(exc, PermissionError) or exc.errno in _ACCESS_DENIED_ERRNOS


def probe_access(
    home: Path | None = None,
    *,
    config: dict[str, Any] | None = None,
    persist: bool = True,
) -> str:
    """Classify Safari cookie access as full_disk_access_missing, readable, or not_used."""
    state = _evaluate_access(home)
    if state == "full_disk_access_missing" and _probe_log_state.get("last") != state:
        _logger.warning(_FULL_DISK_ACCESS_WARNING)
    _probe_log_state["last"] = state
    if persist:
        _persist_last_state(state, config)
    return state


def _evaluate_access(home: Path | None = None) -> str:
    base = home if home is not None else Path.home()
    container = base / "Library/Containers/com.apple.Safari/Data/Library"
    paths = [
        base / "Library/Cookies/Cookies.binarycookies",
        container / "Cookies/Cookies.binarycookies",
    ]
    state: str | None = None
    for root in (container / "WebKit/WebsiteDataStore", base / "Library/WebKit/WebsiteDataStore"):
        try:
            for profile in sorted(root.iterdir()):
                try:
                    if profile.is_dir():
                        paths.append(profile / "Cookies/Cookies.binarycookies")
                except OSError as exc:
                    if _is_access_denied(exc):
                        state = "full_disk_access_missing"
                        break
            if state is not None:
                break
        except FileNotFoundError:
            continue
        except OSError as exc:
            if _is_access_denied(exc):
                state = "full_disk_access_missing"
                break
    if state is None:
        has_readable = False
        for path in paths:
            try:
                with path.open("rb") as stream:
                    stream.read(1)
                has_readable = True
            except FileNotFoundError:
                continue
            except OSError as exc:
                if _is_access_denied(exc):
                    state = "full_disk_access_missing"
                    break
        if state is None:
            state = "readable" if has_readable else "not_used"
    return state


def stores(home: Path) -> list[dict[str, Any]]:
    """Keep containers/profiles distinct; retain protected paths as inaccessible targets."""
    container = home / "Library/Containers/com.apple.Safari/Data/Library"
    paths = [
        (home / "Library/Cookies/Cookies.binarycookies", "Cookies", "Cookies.binarycookies"),
        (container / "Cookies/Cookies.binarycookies", "Default", "Container/Cookies.binarycookies"),
    ]
    for root in (container / "WebKit/WebsiteDataStore", home / "Library/WebKit/WebsiteDataStore"):
        try:
            for profile in sorted(root.iterdir()):
                if profile.is_dir():
                    paths.append(
                        (
                            profile / "Cookies/Cookies.binarycookies",
                            profile.name,
                            str(root.relative_to(home)) + "/Cookies.binarycookies",
                        )
                    )
        except FileNotFoundError:
            continue
        except OSError:
            # A protected profile tree is evidence of incomplete discovery, not
            # an empty browser. It must remain visible to the scan reporter.
            paths.append((root, "Unenumerated", str(root.relative_to(home))))
    result = []
    seen = set()
    for path, profile, store in paths:
        if profile == "Unenumerated":
            result.append(
                {
                    "browser": "Safari",
                    "type": "safari",
                    "path": path,
                    "profile": profile,
                    "store": store,
                    "discovery_status": "inaccessible",
                }
            )
            continue
        try:
            stat = path.stat()
            if not path.is_file():
                continue
            identity = (stat.st_dev, stat.st_ino)
            if identity in seen:
                continue
            seen.add(identity)
        except FileNotFoundError:
            continue
        except OSError:
            pass  # Keep a known protected path so its scan reports inaccessible.
        result.append(
            {
                "browser": "Safari",
                "type": "safari",
                "path": path,
                "profile": profile,
                "store": store,
            }
        )
    return result


def _string(record: bytes, offset: int) -> str:
    if offset < 56 or offset >= len(record):
        raise ValueError("Invalid string offset")
    end = record.find(b"\0", offset)
    if end < 0:
        raise ValueError("Unterminated string")
    value = record[offset:end].decode("utf-8")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Invalid string")
    return value


def scan_cookies(
    path: Path, domain: str | None = None, name: str | None = None
) -> StoreScan[dict[str, Any]]:
    """An invalid or partial decode never counts as an authoritative empty store."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_STORE_BYTES + 1)
    except FileNotFoundError:
        return StoreScan("missing")
    except OSError:
        return StoreScan("inaccessible")
    values: list[dict[str, Any]] = []
    expired = rejected = 0
    try:
        if len(raw) < 8 or len(raw) > _MAX_STORE_BYTES or raw[:4] != b"cook":
            raise ValueError("Unsupported header")
        page_count = struct.unpack_from(">I", raw, 4)[0]
        if page_count > (len(raw) - 8) // 4:
            raise ValueError("Invalid page table")
        cursor = 8 + 4 * page_count
        for index in range(page_count):
            size = struct.unpack_from(">I", raw, 8 + 4 * index)[0]
            if size < 12 or cursor + size > len(raw):
                raise ValueError("Invalid page size")
            page = raw[cursor : cursor + size]
            cursor += size
            if page[:4] != b"\0\0\1\0":
                raise ValueError("Unsupported page")
            count = struct.unpack_from("<I", page, 4)[0]
            if count > (len(page) - 12) // 4:
                raise ValueError("Invalid record table")
            table_end = 12 + 4 * count
            if page[table_end - 4 : table_end] != b"\0" * 4:
                raise ValueError("Invalid page terminator")
            previous_end = table_end
            for number in range(count):
                offset = struct.unpack_from("<I", page, 8 + 4 * number)[0]
                if offset < previous_end or offset + 56 > len(page):
                    raise ValueError("Invalid record offset")
                length = struct.unpack_from("<I", page, offset)[0]
                if length < 56 or offset + length > len(page):
                    raise ValueError("Invalid record length")
                record = page[offset : offset + length]
                previous_end = offset + length
                try:
                    domain_offset, name_offset, path_offset, value_offset = struct.unpack_from(
                        "<4I", record, 16
                    )
                    cookie_domain = _string(record, domain_offset)
                    cookie_name = _string(record, name_offset)
                    bare = domain.lstrip(".").lower() if domain else None
                    if (
                        bare
                        and cookie_domain.lstrip(".").lower() != bare
                        and not cookie_domain.lower().endswith("." + bare)
                    ):
                        continue
                    if name is not None and cookie_name != name:
                        continue
                    expiry, created = struct.unpack_from("<dd", record, 40)
                    if not math.isfinite(expiry) or not math.isfinite(created):
                        raise ValueError("Invalid timestamps")
                    cookie_path = _string(record, path_offset)
                    value = _string(record, value_offset)
                    if not cookie_domain or not cookie_name or not cookie_path.startswith("/"):
                        raise ValueError("Invalid cookie fields")
                    if expiry and expiry + _APPLE_EPOCH <= time.time():
                        expired += 1
                        continue
                    values.append(
                        {
                            "domain": cookie_domain,
                            "name": cookie_name,
                            "value": value,
                            "expires_at": expiry + _APPLE_EPOCH if expiry else None,
                        }
                    )
                except (ValueError, UnicodeError, struct.error):
                    rejected += 1
        return StoreScan(
            "unsupported" if rejected else ("candidates" if values else "empty"),
            values,
            expired_count=expired,
            rejected_count=rejected,
        )
    except (ValueError, UnicodeError, struct.error):
        return StoreScan("unsupported", values, expired_count=expired, rejected_count=rejected + 1)
