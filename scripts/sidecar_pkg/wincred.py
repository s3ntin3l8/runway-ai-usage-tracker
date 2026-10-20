"""Read-only Windows Credential Manager access for the sidecar.

Some Windows CLIs keep their login in the Credential Manager rather than in a
file: the agy (Antigravity) CLI stores its OAuth token under the target
``gemini:antigravity``, and the GitHub CLI stores its token under
``git:https://github.com``.

Entries are read through ``CredReadW`` in ``advapi32`` - no PowerShell, no
extra module, no console window. The earlier route shelled out to
PowerShell's ``Get-StoredCredential``, which needs the CredentialManager
PowerShell module: that module is absent from a stock Windows install, so
every lookup silently returned nothing and the rule was dead on exactly the
platform it was written for.

Only ``ctypes`` is used, and the Win32 types are spelled out rather than
imported from ``ctypes.wintypes``, so this module imports cleanly on the
non-Windows platforms whose sidecars bundle it.
"""

from __future__ import annotations

import ctypes
import json
import platform
from typing import Any

# ``CRED_TYPE_GENERIC`` from wincred.h - the type every CLI here writes.
CRED_TYPE_GENERIC = 1


class _Filetime(ctypes.Structure):
    """Win32 ``FILETIME`` (two 32-bit halves)."""

    _fields_ = [
        ("dwLowDateTime", ctypes.c_uint32),
        ("dwHighDateTime", ctypes.c_uint32),
    ]


class _CredentialW(ctypes.Structure):
    """The Win32 ``CREDENTIALW`` struct (advapi32).

    Field order and widths mirror ``wincred.h``; ``ctypes`` applies the
    platform's own alignment, so the pointer members land where the native
    call expects them.
    """

    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", _Filetime),
        ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.c_uint32),
        ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


def read_credential_bytes(target: str) -> bytes | None:
    """The raw blob of the generic credential *target*, or None when absent.

    None means "no such entry" - a different thing from an entry whose blob
    is empty, which comes back as ``b""``.
    """
    if platform.system() != "Windows":
        return None
    try:
        advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        advapi.CredReadW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.POINTER(_CredentialW)),
        ]
        advapi.CredReadW.restype = ctypes.c_int
        advapi.CredFree.argtypes = [ctypes.c_void_p]

        cred = ctypes.POINTER(_CredentialW)()
        if not advapi.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(cred)):
            return None
        try:
            found = cred.contents
            if not found.CredentialBlob or not found.CredentialBlobSize:
                return b""
            return ctypes.string_at(found.CredentialBlob, found.CredentialBlobSize)
        finally:
            advapi.CredFree(cred)
    except OSError:
        return None


def decode_credential(raw: bytes) -> str:
    """Decode a credential blob to text.

    A blob is raw bytes and its encoding is the writing application's choice:
    the agy CLI writes UTF-8 JSON, the GitHub CLI writes UTF-16LE. A BOM wins;
    otherwise NULs in every odd byte position mark UTF-16LE (the signature of
    ASCII text in UTF-16) and anything else is UTF-8.
    """
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    if len(raw) >= 2 and not any(raw[1::2]):
        return raw.decode("utf-16-le", errors="replace").rstrip("\x00")
    return raw.decode("utf-8", errors="replace")


def read_credential_text(target: str) -> str | None:
    """The text of the generic credential *target*, or None when there is none.

    None means "no such entry"; an entry that exists but holds an empty blob
    comes back as ``""``.
    """
    raw = read_credential_bytes(target)
    if raw is None:
        return None
    return decode_credential(raw)


def read_json_credential(target: str) -> dict[str, Any] | None:
    """The credential *target* parsed as a JSON object, or None.

    None covers both "no such entry" and "the entry is not a JSON object";
    callers that must tell those apart read :func:`read_credential_text`.
    """
    text = read_credential_text(target)
    if text is None:
        return None
    try:
        document = json.loads(text)
    except ValueError:
        return None
    return document if isinstance(document, dict) else None
