"""Fixed resource budgets shared by update checks, downloads, and verification."""

from collections.abc import Iterator
from typing import BinaryIO

MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_BUNDLE_BYTES = 1024 * 1024
MAX_CHECKSUM_BYTES = 4 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024
CHUNK_BYTES = 64 * 1024


class UpdateSizeError(ValueError):
    """Permanent resource-limit failure; never retry or install partial bytes."""


def bounded_chunks(response: BinaryIO, limit: int) -> Iterator[bytes]:
    """Accept at most *limit* actual bytes, independent of Content-Length."""
    headers = getattr(response, "headers", {})
    declared = headers.get("Content-Length")
    try:
        if declared is not None and int(declared) > limit:
            raise UpdateSizeError("Update resource exceeds size limit")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, UpdateSizeError):
            raise
        # Incorrect length metadata cannot relax the actual-byte bound.
    total = 0
    while chunk := response.read(min(CHUNK_BYTES, limit - total + 1)):
        total += len(chunk)
        if total > limit:
            raise UpdateSizeError("Update resource exceeds size limit")
        yield chunk


def bounded_read(response: BinaryIO, limit: int) -> bytes:
    return b"".join(bounded_chunks(response, limit))
