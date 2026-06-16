#!/usr/bin/env python3
"""Generate 16x16 tray icon PNGs.

The glyph is the Runway mark — a vertical capsule "runway" with a dashed white
centerline (see docs/branding.md / assets/logo.svg) — tinted by the per-status
colour, so the desktop tray reads as the same product across states.

Uses Pillow when available (supersampled for crisp edges), otherwise falls back
to a pure-Python PNG writer.
"""

import pathlib
import struct
import zlib

ASSETS_DIR = pathlib.Path(__file__).parent

ICONS = {
    "icon_ok": (0x22, 0xC5, 0x5E),  # green  #22c55e
    "icon_warn": (0xF5, 0x9E, 0x0B),  # amber  #f59e0b
    "icon_err": (0xEF, 0x44, 0x44),  # red    #ef4444
    "icon_paused": (0x9C, 0xA3, 0xAF),  # grey  #9ca3af
}

# Capsule geometry in 16px units (cx, half-width, top, bottom). The straight
# section runs between top+r and bottom-r; r == half-width gives the round caps.
_CX = 8.0
_HALF_W = 3.0
_TOP = 1.5
_BOT = 14.5
# Dashed centerline bands (y ranges, 16px units) — the runway-line motif.
_DASH_BANDS = ((3.6, 5.0), (7.0, 8.5), (10.5, 11.9))
_DASH_HALF_W = 1.0


def _in_capsule(x: float, y: float) -> bool:
    """True when (x, y) lies inside the vertical capsule (SDF clamp trick)."""
    r = _HALF_W
    cy = min(max(y, _TOP + r), _BOT - r)
    return ((x - _CX) ** 2 + (y - cy) ** 2) ** 0.5 <= r


def _in_dash(x: float, y: float) -> bool:
    """True when (x, y) lies on a centerline dash."""
    if abs(x - _CX) > _DASH_HALF_W:
        return False
    return any(y0 <= y <= y1 for y0, y1 in _DASH_BANDS)


def _make_png_pure(r: int, g: int, b: int, size: int = 16) -> bytes:
    """Build a 16x16 RGBA PNG of the capsule glyph, no dependencies."""

    def png_chunk(name: bytes, data: bytes) -> bytes:
        chunk = name + data
        return (
            struct.pack(">I", len(data)) + chunk + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)
        )

    # IHDR — colour type 6 = RGBA (8-bit per channel)
    ihdr_data = struct.pack(">II", size, size) + bytes([8, 6, 0, 0, 0])

    raw_rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            px, py = x + 0.5, y + 0.5
            if _in_capsule(px, py):
                if _in_dash(px, py):
                    row += bytes([255, 255, 255, 255])  # white centerline dash
                else:
                    row += bytes([r, g, b, 255])  # status-tinted capsule
            else:
                row += bytes([0, 0, 0, 0])  # transparent
        raw_rows.append(bytes([0]) + bytes(row))  # filter byte = None (0)

    idat_data = zlib.compress(b"".join(raw_rows), 9)

    png = b"\x89PNG\r\n\x1a\n"
    png += png_chunk(b"IHDR", ihdr_data)
    png += png_chunk(b"IDAT", idat_data)
    png += png_chunk(b"IEND", b"")
    return png


def _make_png_pillow(r: int, g: int, b: int, size: int = 16) -> bytes:
    """Build the capsule glyph with Pillow, supersampled for crisp edges."""
    import io  # noqa: PLC0415

    from PIL import Image, ImageDraw  # noqa: PLC0415

    ss = 8  # supersample factor
    big = size * ss
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Capsule body (status colour).
    draw.rounded_rectangle(
        [(_CX - _HALF_W) * ss, _TOP * ss, (_CX + _HALF_W) * ss, _BOT * ss],
        radius=_HALF_W * ss,
        fill=(r, g, b, 255),
    )
    # Dashed white centerline.
    for y0, y1 in _DASH_BANDS:
        draw.rounded_rectangle(
            [(_CX - _DASH_HALF_W) * ss, y0 * ss, (_CX + _DASH_HALF_W) * ss, y1 * ss],
            radius=_DASH_HALF_W * ss,
            fill=(255, 255, 255, 235),
        )

    img = img.resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def make_png(r: int, g: int, b: int) -> bytes:
    """Create a 16x16 RGBA capsule glyph; use Pillow if available."""
    try:
        return _make_png_pillow(r, g, b)
    except ImportError:
        return _make_png_pure(r, g, b)


def main() -> None:
    for name, (r, g, b) in ICONS.items():
        path = ASSETS_DIR / f"{name}.png"
        path.write_bytes(make_png(r, g, b))
        print(f"Generated {path}")


if __name__ == "__main__":
    main()
