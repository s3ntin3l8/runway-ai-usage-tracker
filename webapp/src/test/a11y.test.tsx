// Accessibility guard: axe-core over the app's main surfaces.
//
// It runs inside the required frontend CI job, so a regression that breaks a
// role, an accessible name, a landmark, a heading order or a label
// association fails the build.
//
// Colour contrast is NOT covered here: jsdom has no CSS engine, so axe
// cannot judge it (it reports the rule as "incomplete"). That check lives in
// `src/styles/contrast.test.ts`, which computes the ratios from the token
// values themselves in both themes.

import axe, { type RunOptions } from 'axe-core';
import { screen, waitFor } from '@testing-library/react';
import { Route, Routes } from 'react-router';
import { renderWithProviders } from '@/test/utils';
import { AppShell } from '@/components/layout/AppShell';
import { HomePage } from '@/features/home/HomePage';
import { ProviderPage } from '@/features/provider/ProviderPage';
import { FleetPage } from '@/features/fleet/FleetPage';
import { SettingsPage } from '@/features/settings/SettingsPage';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), message: vi.fn() },
}));

const AXE_OPTIONS: RunOptions = {
  rules: {
    // The desktop sidebar and the mobile bottom nav are the same five
    // destinations, but both stay in the DOM and CSS hides one of them per
    // breakpoint. jsdom applies no CSS, so both look like duplicate
    // landmarks here; in a browser only one is in the accessibility tree.
    'landmark-unique': { enabled: false },
  },
};

async function expectNoSeriousViolations() {
  const results = await axe.run(document.body, AXE_OPTIONS);
  const blocking = results.violations.filter(
    (violation) => violation.impact === 'serious' || violation.impact === 'critical',
  );
  const summary = blocking
    .map((violation) => `${violation.id}: ${violation.help} → ${violation.nodes.length} node(s)`)
    .join('\n');
  expect(summary).toBe('');
}

function renderSurface(element: React.ReactElement, route: string) {
  return renderWithProviders(
    <Routes>
      <Route element={<AppShell />}>
        <Route path="*" element={element} />
      </Route>
    </Routes>,
    { route },
  );
}

describe('accessibility guard (axe)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.fetchFleetUsage).mockResolvedValue({
      providers: [],
      generated_at: '2026-10-25T00:00:00Z',
    } as never);
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
      providers: [],
    } as never);
    vi.mocked(api.fetchSettings).mockResolvedValue({ version: '0.0.0' } as never);
    vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [] } as never);
    vi.mocked(api.fetchCostForecast).mockResolvedValue({} as never);
    vi.mocked(api.fetchCumulative).mockResolvedValue({} as never);
    vi.mocked(api.fetchForecast).mockResolvedValue({} as never);
    vi.mocked(api.fetchAnomalies).mockResolvedValue({ anomalies: [] } as never);
    vi.mocked(api.fetchDataHealthReport).mockResolvedValue({} as never);
    vi.mocked(api.fetchCredentialInventory).mockResolvedValue({ inventory: [] } as never);
    vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue([] as never);
    vi.mocked(api.fetchPendingUsageSessions).mockResolvedValue({
      sidecars: [],
      providers: [],
      total_events: 0,
    } as never);
    vi.mocked(api.fetchPendingUsageEvents).mockResolvedValue({
      sidecars: [],
      providers: [],
    } as never);
  });

  it('has no serious violations on the app shell', async () => {
    renderSurface(<div>content</div>, '/');
    await waitFor(() => expect(screen.getByRole('main')).toBeInTheDocument());
    await expectNoSeriousViolations();
  });

  it('has no serious violations on the home page', async () => {
    renderSurface(<HomePage />, '/');
    await waitFor(() => expect(screen.getByRole('main')).toBeInTheDocument());
    await expectNoSeriousViolations();
  });

  it('has no serious violations on the provider detail page', async () => {
    renderSurface(<ProviderPage />, '/provider/anthropic');
    await waitFor(() => expect(screen.getByRole('main')).toBeInTheDocument());
    await expectNoSeriousViolations();
  });

  it('has no serious violations on the fleet page', async () => {
    renderSurface(<FleetPage />, '/fleet');
    await waitFor(() => expect(screen.getByRole('main')).toBeInTheDocument());
    await expectNoSeriousViolations();
  });

  it('has no serious violations on the settings page', async () => {
    renderSurface(<SettingsPage />, '/settings/providers');
    await waitFor(() => expect(screen.getByRole('main')).toBeInTheDocument());
    await expectNoSeriousViolations();
  });
});
