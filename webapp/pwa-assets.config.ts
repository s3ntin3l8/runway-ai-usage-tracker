import { defineConfig, minimal2023Preset } from '@vite-pwa/assets-generator/config';
import { combinePresetAndAppleSplashScreens } from '@vite-pwa/assets-generator/config';

// Generates the PWA / home-screen raster icons and Apple splash screens from the
// single canonical mark (public/favicon.svg, derived from assets/logo.svg — see
// docs/branding.md).
// Run via `npm run generate-pwa-assets` (wired into `make logo`); the emitted
// PNGs/ICO live in public/ and are committed, so `vite build` never needs sharp.
//
// The base mark is a dark disc on a transparent canvas. For the maskable and
// Apple icons we fill the canvas with the brand dark (#09090b) so an OS
// squircle/rounded mask never reveals transparent corners.
//
// Apple splash screens: portrait + landscape subset for modern devices (iPhone 14+,
// iPad Pro 11"/12.9", iPad Air 11"/13") to keep the asset footprint reasonable.
// Full matrix (all 50+ devices × both orientations) would add ~100 PNGs; this
// subset adds 22 PNGs (11 unique screen sizes × 2 orientations — several device
// generations share the same physical screen size). All use the brand dark
// background (#0a0a0b matching theme_color) so the launch screen blends
// seamlessly into the installed PWA.

export default defineConfig({
  preset: combinePresetAndAppleSplashScreens(
    {
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
    {
      // Dark brand background matching theme_color (#0a0a0b) so the launch
      // screen blends into the installed PWA without a white flash.
      resizeOptions: { background: '#0a0a0b', fit: 'contain' },
      // Default compression: quality 60, compressionLevel 9.
      padding: 0.3,
      linkMediaOptions: {
        addMediaScreen: true,
        log: true,
      },
    },
    // Most common modern devices (portrait + landscape generated for each).
    [
      'iPhone 16 Pro Max',
      'iPhone 16 Pro',
      'iPhone 16 Plus',
      'iPhone 16',
      'iPhone 16e',
      'iPhone 15 Pro Max',
      'iPhone 15 Pro',
      'iPhone 15 Plus',
      'iPhone 15',
      'iPhone 14 Pro Max',
      'iPhone 14 Pro',
      'iPhone 14 Plus',
      'iPhone 14',
      'iPhone 13 Pro Max',
      'iPhone 13 Pro',
      'iPhone 13',
      'iPhone 13 mini',
      'iPad Pro 12.9"',
      'iPad Pro 11"',
      'iPad Air 13"',
      'iPad Air 11"',
      'iPad mini 8.3"',
    ],
  ),
  images: ['public/favicon.svg'],
});
