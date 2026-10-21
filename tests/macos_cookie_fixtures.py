"""Disposable native cookie fixtures shared without importing test modules."""

import hashlib
import sqlite3
import struct
import time

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

#: A fixed, far-future expiry for blobs whose bytes have to be reproducible.
#: ``safari_blob``'s default expiry is derived from the clock, so a blob
#: built at collection time produced a different parametrised test id in
#: every process -- which pytest-xdist rejects ("different tests were
#: collected between gw0 and gw1").
FIXED_EXPIRY = 4102444800  # 2100-01-01T00:00:00Z


def encrypted(host, value=b"synthetic-session", version=24):
    raw = (hashlib.sha256(host.encode()).digest() if version >= 24 else b"") + value
    pad = padding.PKCS7(128).padder()
    raw = pad.update(raw) + pad.finalize()
    key = hashlib.pbkdf2_hmac("sha1", b"synthetic-key", b"saltysalt", 1003, 16)
    enc = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    return b"v10" + enc.update(raw) + enc.finalize()


def safari_blob(*, expiry=None, domain=".claude.ai", value="synthetic-session"):
    strings = [domain, "sessionKey", "/", value]
    offsets, tail = [], b""
    for text in strings:
        offsets.append(56 + len(tail))
        tail += text.encode() + b"\0"
    record = struct.pack("<4I4I", 56 + len(tail), 0, 0, 0, *offsets)
    record += b"\0" * 8
    record += struct.pack(
        "<dd", (expiry if expiry is not None else time.time() + 3600) - 978307200, 1
    )
    record += tail
    page = b"\0\0\1\0" + struct.pack("<II", 1, 16) + b"\0" * 4 + record
    return b"cook" + struct.pack(">II", 1, len(page)) + page


def chromium_store(path, rows=(), version=24):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('version', ?)", (str(version),))
        conn.execute(
            "CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB, expires_utc INTEGER)"
        )
        conn.executemany("INSERT INTO cookies VALUES (?, 'sessionKey', ?, ?)", rows)
