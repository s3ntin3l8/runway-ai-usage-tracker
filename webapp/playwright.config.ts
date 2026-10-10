import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e/security',
  workers: 1,
  fullyParallel: false,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: { trace: 'retain-on-failure' },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'firefox', use: { ...devices['Desktop Firefox'] } },
    { name: 'webkit', use: { ...devices['Desktop Safari'] } },
  ],
  webServer: [
    { command: 'PYTHONPATH=. .venv/bin/python tests/browser/server.py', cwd: '../', url: 'http://127.0.0.1:18768/', timeout: 60_000, reuseExistingServer: false },
    { command: 'VITE_HOST=0.0.0.0 RUNWAY_API_URL=http://127.0.0.1:18765 npm run dev -- --port 18769 --strictPort', url: 'http://127.0.0.1:18769/', timeout: 60_000, reuseExistingServer: false },
  ],
});
