import { defineConfig, minimal2023Preset } from '@vite-pwa/assets-generator/config';

// Generates the PWA / home-screen raster icons from the single canonical mark
// (public/favicon.svg, derived from assets/logo.svg — see docs/branding.md).
// Run via `npm run generate-pwa-assets` (wired into `make logo`); the emitted
// PNGs/ICO live in public/ and are committed, so `vite build` never needs sharp.
//
// The base mark is a dark disc on a transparent canvas. For the maskable and
// Apple icons we fill the canvas with the brand dark (#09090b) so an OS
// squircle/rounded mask never reveals transparent corners.
export default defineConfig({
  preset: {
    ...minimal2023Preset,
    maskable: {
      ...minimal2023Preset.maskable,
      resizeOptions: { background: '#09090b' },
    },
    apple: {
      ...minimal2023Preset.apple,
      resizeOptions: { background: '#09090b' },
    },
  },
  images: ['public/favicon.svg'],
});
