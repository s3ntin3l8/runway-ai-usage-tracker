// Rasterises the sidecar's native-app brand surfaces from their canonical SVGs.
// Run via `make logo` (see docs/branding.md) — never hand-edit the outputs.
//
// sharp (librsvg) is used rather than a Python SVG renderer because the master
// mark relies on feGaussianBlur glow filters that cairosvg drops. The PNGs land
// in installer/assets/ and are committed, so neither CI nor a PyInstaller
// build ever needs sharp. installer/generate_app_icons.py then packs
// app-icon-1024.png into .icns/.ico and converts the NSIS PNGs to BMP.

import { mkdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import sharp from 'sharp';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const out = path.join(root, 'installer', 'assets');

// [source svg, output png, width, height]
const jobs = [
  ['assets/logo.svg', 'app-icon-1024.png', 1024, 1024],
  ['assets/installer/dmg-background.svg', 'dmg-background.png', 760, 480],
  ['assets/installer/dmg-background.svg', 'dmg-background@2x.png', 1520, 960],
  ['assets/installer/nsis-sidebar.svg', 'installer-sidebar.png', 164, 314],
  ['assets/installer/nsis-header.svg', 'installer-header.png', 150, 57],
];

await mkdir(out, { recursive: true });
for (const [src, dest, width, height] of jobs) {
  const srcPath = path.join(root, src);
  // Render at the target pixel density so text and hairlines stay sharp
  // instead of being upscaled from the SVG's nominal size.
  const { width: nominal } = await sharp(srcPath).metadata();
  const density = Math.round((72 * width) / (nominal ?? width));
  await sharp(srcPath, { density })
    .resize(width, height, { fit: 'fill' })
    .png({ compressionLevel: 9 })
    .toFile(path.join(out, dest));
  console.log(`${src} -> installer/assets/${dest} (${width}x${height})`);
}
