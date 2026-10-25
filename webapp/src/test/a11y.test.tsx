// Accessibility guard: axe-core over the app's main surfaces.
//
// It runs inside the required frontend CI job, so a regression that breaks a
// role, an accessible name, a landmark, a heading order or a label
// association fails the build.
//
// The guard asserts on RULE IDS, not on an impact tier. An impact tier is a
// poor proxy: in axe-core 4.14 `heading-order`, `region`, `landmark-one-main`
// and `landmark-unique` are all `moderate`, so a "serious or critical" filter
// never fails on them — the landmark/heading half of the guarantee was
// unenforceable. Here every violation fails the build unless its rule id is in
// EXCEPTED_RULES, where each entry carries the reason it is not enforced.
//
// The surfaces are rendered with POPULATED api mocks and are waited out
// before the scan. An empty mock, or a scan taken while the queries are
// still loading, renders a different tree from the one a user meets: the
// provider tabs, the sessions table, the cost split and the confirm dialogs
// are where this PR reshaped the markup, and they are only in the DOM when
// there is data and the queries have settled.
//
// Colour contrast is NOT covered here: jsdom has no CSS engine, so axe
// cannot judge it (it reports the rule as "incomplete"). That check lives in
// `src/styles/contrast.test.ts`, which computes the ratios from the token
// values themselves in both themes.

import axe, { type AxeResults, type RunOptions } from 'axe-core';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes } from 'react-router';
import type { QueryClient } from '@tanstack/react-query';
import { renderWithProviders } from '@/test/utils';
import { AppShell } from '@/components/layout/AppShell';
import { ThemeProvider } from '@/hooks/useTheme';
import { HomePage } from '@/features/home/HomePage';
import { ProviderPage } from '@/features/provider/ProviderPage';
import { FleetPage } from '@/features/fleet/FleetPage';
import { SettingsPage } from '@/features/settings/SettingsPage';
import { HistoryPage } from '@/features/history/HistoryPage';
import { InsightsPage } from '@/features/insights/InsightsPage';
import {
  costForecast,
  cumulativeResponse,
  fleetEntry,
  fleetResponse,
  forecastEntry,
  forecastResponse,
  historyChart,
  sessionsPaginated,
} from '@/features/provider/test-fixtures';
import * as api from '@/api/endpoints';

// Every endpoint gets a stable mock whose default resolves to an empty
// object. A default matters: an unmocked endpoint resolves to `undefined`,
// which React Query rejects and which unmounts the tree mid-render — the
// scan would then run against an empty document and pass for the wrong
// reason.
vi.mock('@/api/endpoints', async () => {
  const actual = await vi.importActual<typeof import('@/api/endpoints')>('@/api/endpoints');
  const mocks: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(actual)) {
    mocks[key] = typeof value === 'function' ? vi.fn(async () => ({})) : value;
  }
  return { ...actual, ...mocks };
});

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), message: vi.fn() },
}));

// ECharts needs a real canvas, which jsdom does not provide: the library
// throws inside its own effect, React tears the tree down and the scan would
// run against an empty document. The stub keeps the part axe can judge — the
// container's role and accessible name — and drops the canvas.
vi.mock('@/components/charts/EChart', () => ({
  EChart: ({ ariaLabel }: { ariaLabel?: string }) => (
    <div role={ariaLabel ? 'img' : undefined} aria-label={ariaLabel} />
  ),
}));

/**
 * Rules this guard does not enforce, and why.
 *
 * Every other rule id fails the build. Keep this list short and every entry
 * justified — an exception here is a hole in the guarantee.
 */
const EXCEPTED_RULES: Record<string, string> = {
  // The desktop sidebar and the mobile bottom nav are the same five
  // destinations, but both stay in the DOM and CSS hides one of them per
  // breakpoint (sidebar `hidden lg:flex`, bottom nav `lg:hidden`). jsdom
  // applies no CSS, so both look like duplicate landmarks here; in a browser
  // only one is in the accessibility tree.
  'landmark-unique': 'desktop sidebar and mobile bottom nav are both in the DOM in jsdom',
};

const AXE_OPTIONS: RunOptions = {
  rules: Object.fromEntries(
    Object.keys(EXCEPTED_RULES).map((id) => [id, { enabled: false }]),
  ),
};

/** The violations this guard enforces: everything outside the exception list. */
function blockingViolations(results: AxeResults) {
  return results.violations.filter((violation) => !(violation.id in EXCEPTED_RULES));
}

async function expectNoViolations() {
  const results = await axe.run(document.body, AXE_OPTIONS);
  const blocking = blockingViolations(results);
  const summary = blocking
    .map(
      (violation) =>
        `${violation.id} [${violation.impact}]: ${violation.help} → ${violation.nodes.length} node(s)` +
        violation.nodes
          .slice(0, 3)
          .map((node) => `\n    ${node.target.join(' ')}\n    ${node.html.slice(0, 700)}`)
          .join(''),
    )
    .join('\n');
  expect(summary).toBe('');
}

/**
 * Waits for every query to settle.
 *
 * Without this the scan runs against the skeleton, the half-filled account
 * list or the placeholder-only select — a tree the user never sees, and one
 * that hides exactly the defects this guard exists to catch.
 */
async function settle(client: QueryClient) {
  await waitFor(() => expect(client.isFetching()).toBe(0));
  // One more tick: a query that resolves can enqueue the next one (a
  // dependent query), and `isFetching` reads 0 in between.
  await waitFor(() => expect(client.isFetching()).toBe(0));
}

function renderSurface(pathPattern: string, element: React.ReactElement, route: string) {
  return renderWithProviders(
    <ThemeProvider>
      <Routes>
        <Route element={<AppShell />}>
          <Route path={pathPattern} element={element} />
        </Route>
      </Routes>
    </ThemeProvider>,
    { route },
  );
}

/** Populated api responses — the same shapes the app sees in production. */
function mockPopulatedApis() {
  vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([fleetEntry()]) as never);
  vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
    providers: [
      {
        provider_id: 'anthropic',
        name: 'Anthropic',
        archived: false,
        account_count: 1,
        accounts: [
          {
            account_id: 'me@example.com',
            account_label: 'Primary',
            api_key_set: true,
            session_cookie_set: false,
            billing_type: 'subscription',
            archived: false,
          },
        ],
      },
    ],
  } as never);
  vi.mocked(api.fetchSettings).mockResolvedValue({
    version: '1.2.3',
    latest_version: '1.2.3',
    update_available: false,
  } as never);
  vi.mocked(api.fetchSidecars).mockResolvedValue({
    sidecars: [
      {
        sidecar_id: 'laptop',
        hostname: 'workstation.local',
        custom_name: 'Workstation',
        tags: ['dev'],
        first_seen: new Date(Date.now() - 86_400_000).toISOString(),
        last_seen: new Date().toISOString(),
        sidecar_version: '1.0.0',
        collection_enabled: true,
      },
    ],
  } as never);
  vi.mocked(api.fetchCostForecast).mockResolvedValue(costForecast() as never);
  vi.mocked(api.fetchCumulative).mockResolvedValue(cumulativeResponse() as never);
  vi.mocked(api.fetchForecast).mockResolvedValue(
    forecastResponse([forecastEntry()]) as never,
  );
  vi.mocked(api.fetchAnomalies).mockResolvedValue({ anomalies: [] } as never);
  vi.mocked(api.fetchDataHealthReport).mockResolvedValue({
    providers: [],
    sidecars: [],
    generated_at: new Date().toISOString(),
  } as never);
  vi.mocked(api.fetchCredentialInventory).mockResolvedValue({
    providers: [],
    machines: [],
    unmapped_count: 0,
    rule_count: 0,
    pending_usage_events: 0,
  } as never);
  vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({
    items: [],
    counts_by_sidecar: {},
  } as never);
  vi.mocked(api.fetchPendingUsageSessions).mockResolvedValue({
    sidecars: [],
    providers: [],
    total_events: 0,
  } as never);
  vi.mocked(api.fetchPendingUsageEvents).mockResolvedValue({
    sidecars: [],
    providers: [],
  } as never);
  vi.mocked(api.fetchSessionsPaginated).mockResolvedValue(
    sessionsPaginated(3, 3) as never,
  );
  vi.mocked(api.fetchHeatmap).mockResolvedValue({
    tz: 'America/New_York',
    cells: [],
  } as never);
  vi.mocked(api.fetchHistoryChart).mockResolvedValue(historyChart(true) as never);
  vi.mocked(api.fetchHistoryWindows).mockResolvedValue({
    windows: [],
    generated_at: new Date().toISOString(),
  } as never);
  vi.mocked(api.fetchHistoryDeltas).mockResolvedValue({
    token_delta_total: 1200,
    cost_delta_total: 3.5,
    cost_cache_total: 0.5,
    token_cache_total: 200,
    critical_series_count: 2,
  } as never);
  vi.mocked(api.fetchGlobalStats).mockResolvedValue({
    lifetime: {
      tokens_total: 12345,
      tokens_input: 8000,
      tokens_output: 3000,
      tokens_cache_read: 1000,
      tokens_cache_create: 345,
      tokens_reasoning: 0,
      tokens_cache: 1345,
      cost_usd: 12.5,
      cost_cache: 1.25,
      msgs: 42,
    },
    sessions: { count: 7, avg_cost: 1.79, avg_tokens: 1763 },
    cache_hit_ratio: 0.32,
    distinct_models: 3,
    distinct_providers: 1,
    busiest_day: null,
    busiest_hour: null,
    generated_at: new Date().toISOString(),
  } as never);
  vi.mocked(api.fetchTopProjects).mockResolvedValue({
    projects: [],
    metric: 'tokens',
    generated_at: new Date().toISOString(),
  } as never);
  vi.mocked(api.fetchTopSessions).mockResolvedValue({
    sessions: [],
    metric: 'tokens',
    generated_at: new Date().toISOString(),
  } as never);
  vi.mocked(api.fetchTopHosts).mockResolvedValue({
    hosts: [],
    metric: 'tokens',
    generated_at: new Date().toISOString(),
  } as never);
  vi.mocked(api.fetchTopModels).mockResolvedValue({
    models: [],
    metric: 'tokens',
    generated_at: new Date().toISOString(),
  } as never);
  vi.mocked(api.fetchTopTools).mockResolvedValue({
    tools: [],
    generated_at: new Date().toISOString(),
  } as never);
}

describe('accessibility guard (axe)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockPopulatedApis();
  });

  it('has no violations on the app shell', async () => {
    const { client } = renderSurface('/', <div>content</div>, '/');
    await waitFor(() => expect(screen.getByRole('main')).toBeInTheDocument());
    await settle(client);
    await expectNoViolations();
  });

  it('has no violations on the home page', async () => {
    const { client } = renderSurface('/', <HomePage />, '/');
    await waitFor(() => expect(screen.getByRole('main')).toBeInTheDocument());
    await settle(client);
    await expectNoViolations();
  });

  it('has no violations on the provider overview tab', async () => {
    const { client } = renderSurface(
      'provider/:providerId',
      <ProviderPage />,
      '/provider/anthropic',
    );
    await waitFor(() => expect(screen.getByRole('main')).toBeInTheDocument());
    await settle(client);
    await expectNoViolations();
  });

  it('has no violations on the provider sessions tab', async () => {
    const { client } = renderSurface(
      'provider/:providerId',
      <ProviderPage />,
      '/provider/anthropic?tab=sessions',
    );
    await waitFor(() => expect(screen.getByRole('main')).toBeInTheDocument());
    await settle(client);
    await expectNoViolations();
  });

  it('has no violations on the provider cost tab', async () => {
    const { client } = renderSurface(
      'provider/:providerId',
      <ProviderPage />,
      '/provider/anthropic?tab=cost',
    );
    await waitFor(() => expect(screen.getByRole('main')).toBeInTheDocument());
    await settle(client);
    await expectNoViolations();
  });

  it('has no violations on the fleet page', async () => {
    const { client } = renderSurface('/fleet', <FleetPage />, '/fleet');
    await waitFor(() => expect(screen.getByRole('main')).toBeInTheDocument());
    await settle(client);
    await expectNoViolations();
  });

  it('has no violations on the settings page', async () => {
    const { client } = renderSurface('/settings/*', <SettingsPage />, '/settings/providers');
    await waitFor(() => expect(screen.getByRole('main')).toBeInTheDocument());
    await settle(client);
    await expectNoViolations();
  });

  it('has no violations on the history page', async () => {
    const { client } = renderSurface('/history', <HistoryPage />, '/history');
    await waitFor(() => expect(screen.getByRole('main')).toBeInTheDocument());
    await settle(client);
    await expectNoViolations();
  });

  it('has no violations on the insights page', async () => {
    const { client } = renderSurface('/insights', <InsightsPage />, '/insights');
    await waitFor(() => expect(screen.getByRole('main')).toBeInTheDocument());
    await settle(client);
    await expectNoViolations();
  });

  it('has no violations in the destructive-action confirm dialog', async () => {
    const user = userEvent.setup();
    const { client } = renderSurface(
      'provider/:providerId',
      <ProviderPage />,
      '/provider/anthropic',
    );
    await waitFor(() => expect(screen.getByRole('main')).toBeInTheDocument());
    await settle(client);
    await user.click(screen.getByRole('button', { name: 'More provider actions' }));
    await user.click(await screen.findByRole('menuitem', { name: 'Archive provider' }));
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());
    await expectNoViolations();
  });
});
