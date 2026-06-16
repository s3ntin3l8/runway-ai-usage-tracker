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
        name: 'Runway',
        short_name: 'Runway',
        description: 'Local-first monitoring for AI provider quotas and usage.',
        theme_color: '#09090b',
        background_color: '#09090b',
        display: 'standalone',
        start_url: '/',
        scope: '/',
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
      },
      workbox: {
        globPatterns: ['**/*.{js,css,html,svg,png,ico,woff2}'],
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
