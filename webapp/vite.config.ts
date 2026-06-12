/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
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
        manualChunks: {
          echarts: ['echarts'],
        },
      },
    },
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
  },
});
