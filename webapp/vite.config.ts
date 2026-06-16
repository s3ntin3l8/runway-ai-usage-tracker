/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { VitePWA } from 'vite-plugin-pwa';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    VitePWA({
      // New versions take over on the next navigation (no update prompt UI).
      registerType: 'autoUpdate',
      // We call registerSW() ourselves from main.tsx (bundled into a hashed
      // /assets script), so the strict CSP (script-src 'self') stays intact —
      // never let the plugin inject an inline registration script.
      injectRegister: null,
      // Precached so the install + offline shell has its icons. The raster
      // icons themselves are committed in public/ (run `make logo` to refresh)
      // and picked up by globPatterns below.
      includeAssets: ['favicon.svg', 'apple-touch-icon-180x180.png'],
      manifest: {
        id: '/',
        name: 'Runway',
        short_name: 'Runway',
        description: 'Local-first monitoring for AI provider quotas and usage.',
        lang: 'en',
        dir: 'ltr',
        // Dark base (#0a0a0b == --canvas dark in tokens.css).
        theme_color: '#0a0a0b',
        background_color: '#0a0a0b',
        display: 'standalone',
        orientation: 'any',
        start_url: '/',
        scope: '/',
        categories: ['productivity', 'utilities'],
        icons: [
          { src: 'pwa-64x64.png', sizes: '64x64', type: 'image/png' },
          { src: 'pwa-192x192.png', sizes: '192x192', type: 'image/png' },
          { src: 'pwa-512x512.png', sizes: '512x512', type: 'image/png' },
          {
            src: 'maskable-icon-512x512.png',
            sizes: '512x512',
            type: 'image/png',
            purpose: 'maskable',
          },
        ],
        // Quick-launch the main routes from the installed-app icon.
        shortcuts: [
          { name: 'Dashboard', short_name: 'Dashboard', url: '/' },
          { name: 'History', short_name: 'History', url: '/history' },
          { name: 'Insights', short_name: 'Insights', url: '/insights' },
          { name: 'Fleet', short_name: 'Fleet', url: '/fleet' },
          { name: 'Settings', short_name: 'Settings', url: '/settings' },
        ],
        // Desktop ("wide") install-dialog previews. These mirror a subset of
        // assets/screenshots/ (copied into public/screenshots/). We have no
        // narrow/mobile captures, so the mobile install dialog won't show them.
        screenshots: [
          {
            src: 'screenshots/dashboard.png',
            sizes: '1440x969',
            type: 'image/png',
            form_factor: 'wide',
            label: 'Dashboard',
          },
          {
            src: 'screenshots/history.png',
            sizes: '1440x900',
            type: 'image/png',
            form_factor: 'wide',
            label: 'Usage history',
          },
          {
            src: 'screenshots/fleet.png',
            sizes: '1440x900',
            type: 'image/png',
            form_factor: 'wide',
            label: 'Fleet management',
          },
          {
            src: 'screenshots/settings.png',
            sizes: '1440x931',
            type: 'image/png',
            form_factor: 'wide',
            label: 'Settings',
          },
        ],
      },
      workbox: {
        globPatterns: ['**/*.{js,css,html,svg,png,ico,woff2}'],
        // Install-dialog screenshots are fetched by the browser/OS, not the app
        // shell — keep them out of the precache.
        globIgnores: ['**/screenshots/*'],
        // Offline SPA shell — but never let the SW answer API calls.
        navigateFallback: '/index.html',
        navigateFallbackDenylist: [/^\/api\//],
        cleanupOutdatedCaches: true,
      },
    }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    // Bind on all interfaces so the dev server is reachable when Runway runs
    // on a remote/headless host (matches APP_HOST=0.0.0.0). Override the port
    // with VITE_PORT if 5173 is taken.
    host: true,
    port: Number(process.env.VITE_PORT ?? 5173),
    proxy: {
      // Dev: Vite serves the SPA, FastAPI (make dev) serves the API. The
      // proxy originates from localhost so the server's localhost admin
      // bypass applies — no key setup needed in dev. Override the target
      // with RUNWAY_API_URL when the backend runs elsewhere.
      '/api': process.env.RUNWAY_API_URL ?? 'http://127.0.0.1:8765',
    },
  },
  build: {
    // NOTE: repo-root dist/ belongs to the sidecar PyInstaller build; this
    // output must stay inside webapp/.
    outDir: 'dist',
    rollupOptions: {
      output: {
        // Function form (not the record form) so the type stays valid under
        // Vite 8's Rollup: split echarts into its own chunk to keep the main
        // bundle small.
        manualChunks(id) {
          if (id.includes('node_modules/echarts')) return 'echarts';
        },
      },
    },
  },
  test: {
    // jsdom by default so component tests render; pure-logic tests run fine in
    // it too. `globals: true` enables RTL auto-cleanup + jest-dom matchers.
    environment: 'jsdom',
    globals: true,
    setupFiles: ['src/test/setup.ts'],
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
    coverage: {
      provider: 'v8',
      // json-summary feeds the CI coverage gate (coverage/coverage-summary.json);
      // lcov is what Codecov ingests; text prints a local summary.
      reporter: ['text', 'json-summary', 'lcov'],
      reportsDirectory: './coverage',
      include: ['src/**/*.{ts,tsx}'],
      exclude: [
        'src/**/*.test.{ts,tsx}',
        'src/test/**',
        'src/main.tsx',
        'src/**/*.d.ts',
        'src/api/types.ts',
      ],
    },
  },
});
