#!/usr/bin/env python3
"""Generate 16x16 tray icon PNGs.

Uses Pillow when available, otherwise falls back to a pure-Python PNG writer.
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


def _make_png_pure(r: int, g: int, b: int, size: int = 16) -> bytes:
    """Build a minimal 16x16 RGBA PNG with a filled circle, no dependencies."""

    def png_chunk(name: bytes, data: bytes) -> bytes:
        chunk = name + data
        return (
            struct.pack(">I", len(data)) + chunk + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)
        )

    # IHDR
    ihdr_data = struct.pack(
        ">IIBBBBB", size, size, 8, 2, 0, 0, 0
    )  # 8-bit RGB + alpha=RGBA -> use 6
    # Actually use colour type 6 = RGBA (8-bit per channel)
    ihdr_data = struct.pack(">II", size, size) + bytes([8, 6, 0, 0, 0])

    cx = cy = size / 2
    radius = size / 2 - 1  # 1px inset

    raw_rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            dx = x + 0.5 - cx
            dy = y + 0.5 - cy
            dist = (dx * dx + dy * dy) ** 0.5
            if dist <= radius:
                row += bytes([r, g, b, 255])
            else:
                row += bytes([0, 0, 0, 0])
        raw_rows.append(bytes([0]) + bytes(row))  # filter byte = None (0)

    idat_data = zlib.compress(b"".join(raw_rows), 9)

    png = b"\x89PNG\r\n\x1a\n"
    png += png_chunk(b"IHDR", ihdr_data)
    png += png_chunk(b"IDAT", idat_data)
    png += png_chunk(b"IEND", b"")
    return png


def _make_png_pillow(r: int, g: int, b: int, size: int = 16) -> bytes:
    """Build icon using Pillow (preferred when available)."""
    import io  # noqa: PLC0415

    from PIL import Image, ImageDraw  # noqa: PLC0415

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((1, 1, size - 2, size - 2), fill=(r, g, b, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def make_png(r: int, g: int, b: int) -> bytes:
    """Create a 16x16 RGBA PNG circle; use Pillow if available."""
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
