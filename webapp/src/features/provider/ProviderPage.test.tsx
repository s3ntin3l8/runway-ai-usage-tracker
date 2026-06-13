import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes } from 'react-router';
import { renderWithProviders } from '@/test/utils';
import { ProviderPage } from './ProviderPage';
import * as api from '@/api/endpoints';
import { fleetEntry, fleetResponse, limitCard, providerConfigs } from './test-fixtures';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), message: vi.fn() },
}));

// Stub the Overview tab so we exercise ProviderPage's own logic (header,
// account select, tabs, mutations) without its heavy chart-bearing subtree.
vi.mock('./OverviewTab', () => ({
  OverviewTab: ({ entry }: { entry: { account_id: string } }) => (
    <div data-testid="overview-tab">overview:{entry.account_id}</div>
  ),
}));
vi.mock('./ActivityTab', () => ({ ActivityTab: () => <div data-testid="activity-tab" /> }));
vi.mock('./EventsTab', () => ({ EventsTab: () => <div data-testid="events-tab" /> }));
vi.mock('./ForecastTab', () => ({ ForecastTab: () => <div data-testid="forecast-tab" /> }));
vi.mock('./CostTab', () => ({ CostTab: () => <div data-testid="cost-tab" /> }));
vi.mock('./DebugTab', () => ({ DebugTab: () => <div data-testid="debug-tab" /> }));

const renderPage = (route = '/provider/anthropic') =>
  renderWithProviders(
    <Routes>
      <Route path="/provider/:providerId" element={<ProviderPage />} />
    </Routes>,
    { route },
  );

describe('ProviderPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue(providerConfigs() as never);
  });

  it('renders the provider name and the default Overview tab', async () => {
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([fleetEntry()]));
    renderPage();
    expect(await screen.findByTestId('overview-tab')).toHaveTextContent('me@example.com');
    expect(screen.getByRole('heading', { name: 'Anthropic' })).toBeInTheDocument();
  });

  it('shows the empty state when the provider has no fleet entry', async () => {
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([]));
    renderPage();
    expect(await screen.findByText(/no data for this provider/i)).toBeInTheDocument();
  });

  it('switches tabs and reflects them in the URL', async () => {
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([fleetEntry()]));
    renderPage();
    await screen.findByTestId('overview-tab');

    await userEvent.click(screen.getByRole('tab', { name: 'Cost' }));
    expect(await screen.findByTestId('cost-tab')).toBeInTheDocument();
  });

  it('respects the tab query param on first render', async () => {
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([fleetEntry()]));
    renderPage('/provider/anthropic?tab=events');
    expect(await screen.findByTestId('events-tab')).toBeInTheDocument();
  });

  it('triggers collection and toasts on success', async () => {
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([fleetEntry()]));
    vi.mocked(api.collectProvider).mockResolvedValue({ status: 'ok' } as never);
    renderPage();
    await screen.findByTestId('overview-tab');

    await userEvent.click(screen.getByRole('button', { name: /collect now/i }));
    await waitFor(() =>
      expect(api.collectProvider).toHaveBeenCalledWith('anthropic', 'me@example.com'),
    );
  });

  it('clears the failure state via the reset control', async () => {
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([fleetEntry()]));
    vi.mocked(api.resetProvider).mockResolvedValue({ status: 'ok' } as never);
    renderPage();
    await screen.findByTestId('overview-tab');

    await userEvent.click(screen.getByRole('button', { name: /clear failure state/i }));
    await waitFor(() =>
      expect(api.resetProvider).toHaveBeenCalledWith('anthropic', 'me@example.com'),
    );
  });

  it('renders an account selector when multiple accounts exist', async () => {
    const a = fleetEntry({ account_id: 'a@x.com', critical_gauge: limitCard({ account_id: 'a@x.com', account_label: 'Acct A' }) });
    const b = fleetEntry({ account_id: 'b@x.com', critical_gauge: limitCard({ account_id: 'b@x.com', account_label: 'Acct B' }) });
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([a, b]));
    renderPage();
    await screen.findByTestId('overview-tab');
    expect(screen.getByRole('combobox')).toBeInTheDocument();
  });

  it('selects the account named in the ?account param', async () => {
    const a = fleetEntry({ account_id: 'a@x.com' });
    const b = fleetEntry({ account_id: 'b@x.com' });
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([a, b]));
    renderPage('/provider/anthropic?account=b@x.com');
    expect(await screen.findByTestId('overview-tab')).toHaveTextContent('b@x.com');
  });
});
