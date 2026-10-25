import { defineConfig, devices } from '@playwright/test';

// The UX suite runs against the DEMO instance. The security suite's server
// (`tests/browser/server.py`) boots an empty database, and a layout
// assertion against an empty app proves nothing: the overflowing column
// only shows up once there are rows in it. The demo seeds six providers, two
// sidecars and sixty days of history, and serves the built SPA.
//
// Chromium only: this suite is about layout, and the security suite already
// covers the three-browser matrix.
export default defineConfig({
  testDir: './e2e/ux',
  workers: 1,
  fullyParallel: false,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: { trace: 'retain-on-failure' },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: [
    {
      command: 'PYTHONPATH=. .venv/bin/python -m scripts.demo.instance',
      cwd: '../',
      url: 'http://127.0.0.1:3965/',
      // The demo seeds the database, and it rebuilds the SPA when the
      // sources are newer than the bundle.
      timeout: 300_000,
      reuseExistingServer: false,
    },
  ],
});
