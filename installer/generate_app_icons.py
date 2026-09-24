#!/usr/bin/env python3
"""Pack the sidecar's native-app icons from the rasterised master mark.

Inputs (rendered from SVG by ``webapp/scripts/render-brand-rasters.mjs``,
both steps run by ``make logo`` — see docs/branding.md):

- ``installer/assets/app-icon-1024.png`` — assets/logo.svg at 1024x1024
- ``installer/assets/installer-{sidebar,header}.png`` — the NSIS wizard art

Outputs (committed; consumed by the PyInstaller specs, the NSIS script and
the DMG build — never hand-edit them). Build-only art stays in
``installer/assets/`` so it is not bundled into the app; only the tray logo
lands in ``sidecar_app/assets/`` (which the specs ship wholesale):

- ``installer/assets/app.icns``  — macOS bundle icon (16…1024 incl. @2x slots)
- ``installer/assets/app.ico``   — Windows exe / installer / uninstaller icon
- ``installer/assets/installer-{sidebar,header}.bmp`` — 24-bit BMPs, the only
  format NSIS takes
- ``sidecar_app/assets/tray-logo.png`` — 256px full-colour mark for the
  Windows/Linux tray
"""

import pathlib
import sys

from PIL import Image

ASSETS_DIR = pathlib.Path(__file__).parent / "assets"
# The tray logo is bundled into the app, so it lives with the runtime assets.
TRAY_LOGO = pathlib.Path(__file__).parent.parent / "sidecar_app" / "assets" / "tray-logo.png"
MASTER = ASSETS_DIR / "app-icon-1024.png"

ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
# Pillow writes every ICNS slot it knows (16…1024, including the @2x
# variants) from the largest image; the extra sizes are downscaled with a
# high-quality filter first so small slots stay crisp.
ICNS_SIZES = [16, 32, 64, 128, 256, 512, 1024]
BMP_BACKGROUND = (0x09, 0x09, 0x0B)  # brand dark; BMP has no alpha


def _resized(img: Image.Image, size: int) -> Image.Image:
    return img.resize((size, size), Image.Resampling.LANCZOS)


def main() -> int:
    if not MASTER.exists():
        print(
            f"missing {MASTER.name} — run `make logo` (renders it from assets/logo.svg)",
            file=sys.stderr,
        )
        return 1
    master = Image.open(MASTER).convert("RGBA")

    master.save(
        ASSETS_DIR / "app.icns",
        format="ICNS",
        append_images=[_resized(master, s) for s in ICNS_SIZES if s != master.width],
    )
    master.save(ASSETS_DIR / "app.ico", format="ICO", sizes=ICO_SIZES)
    _resized(master, 256).save(TRAY_LOGO, optimize=True)

    for name in ("installer-sidebar", "installer-header"):
        src = Image.open(ASSETS_DIR / f"{name}.png").convert("RGBA")
        flat = Image.new("RGB", src.size, BMP_BACKGROUND)
        flat.paste(src, mask=src.getchannel("A"))
        flat.save(ASSETS_DIR / f"{name}.bmp", format="BMP")

    for out in ("app.icns", "app.ico", "installer-sidebar.bmp", "installer-header.bmp"):
        print(f"wrote installer/assets/{out}")
    print("wrote sidecar_app/assets/tray-logo.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
