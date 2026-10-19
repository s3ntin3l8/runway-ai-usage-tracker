"""Read-only Safari binary-cookie discovery and bounded structural decoding."""

import math
import struct
import time
from pathlib import Path
from typing import Any

from scripts.sidecar_pkg.cookie_scan import StoreScan

_APPLE_EPOCH = 978307200
_MAX_STORE_BYTES = 64 * 1024 * 1024


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
    try:
        if len(raw) < 8 or len(raw) > _MAX_STORE_BYTES or raw[:4] != b"cook":
            raise ValueError("Unsupported header")
        page_count = struct.unpack_from(">I", raw, 4)[0]
        if page_count > (len(raw) - 8) // 4:
            raise ValueError("Invalid page table")
        cursor = 8 + 4 * page_count
        values = []
        expired = 0
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
                domain_offset, name_offset, path_offset, value_offset = struct.unpack_from(
                    "<4I", record, 16
                )
                cookie_domain, cookie_name, cookie_path, value = (
                    _string(record, item)
                    for item in (domain_offset, name_offset, path_offset, value_offset)
                )
                expiry, created = struct.unpack_from("<dd", record, 40)
                if not math.isfinite(expiry) or not math.isfinite(created):
                    raise ValueError("Invalid timestamps")
                if not cookie_domain or not cookie_name or not cookie_path.startswith("/"):
                    raise ValueError("Invalid cookie fields")
                bare = domain.lstrip(".").lower() if domain else None
                if (
                    bare
                    and cookie_domain.lstrip(".").lower() != bare
                    and not cookie_domain.lower().endswith("." + bare)
                ):
                    continue
                if name is not None and cookie_name != name:
                    continue
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
        return StoreScan("candidates" if values else "empty", values, expired_count=expired)
    except (ValueError, UnicodeError, struct.error):
        return StoreScan("unsupported")
