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
      {/* A home route so the Back control has somewhere to land. */}
      <Route path="/" element={<div data-testid="home-route" />} />
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

  it('shows an error state, not the empty state, when the request fails', async () => {
    // The audit finding: a failed request rendered "No data for this
    // provider", which reads as "this provider has no usage" rather than
    // "the page could not load".
    vi.mocked(api.fetchFleetUsage).mockRejectedValue(new Error('boom'));
    renderPage();
    expect(await screen.findByText(/could not load this provider/i)).toBeInTheDocument();
    expect(screen.getByText('boom')).toBeInTheDocument();
    expect(screen.queryByText(/no data for this provider/i)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('says where a merged-away account went instead of "no data"', async () => {
    // The account was merged away: it holds nothing, so it has no fleet
    // entry. The operator followed a link to it — saying where it went beats
    // an unexplained empty state.
    vi.mocked(api.fetchFleetUsage).mockResolvedValue({
      ...fleetResponse([]),
      merged_accounts: [
        {
          provider_id: 'anthropic',
          account_id: 'old@example.com',
          destination_account_id: 'me@example.com',
          merged_at: '2026-10-20T10:00:00+00:00',
        },
      ],
    });
    renderPage('/provider/anthropic?account=old@example.com');
    expect(await screen.findByText(/merged away/i)).toBeInTheDocument();
    expect(screen.getByText(/me@example\.com/)).toBeInTheDocument();
    expect(screen.queryByText(/no data for this provider/i)).not.toBeInTheDocument();
  });

  it('does not show the destination data under a merged-away account', async () => {
    // The destination usually has a fleet entry of its own. Falling back to
    // it would show the destination's usage under the merged-away account's
    // name — the very confusion the merge notice exists to prevent.
    vi.mocked(api.fetchFleetUsage).mockResolvedValue({
      ...fleetResponse([fleetEntry({ account_id: 'me@example.com' })]),
      merged_accounts: [
        {
          provider_id: 'anthropic',
          account_id: 'old@example.com',
          destination_account_id: 'me@example.com',
          merged_at: '2026-10-20T10:00:00+00:00',
        },
      ],
    });
    renderPage('/provider/anthropic?account=old@example.com');
    expect(await screen.findByText(/merged away/i)).toBeInTheDocument();
    expect(screen.queryByTestId('overview-tab')).not.toBeInTheDocument();
  });

  it('says the cookie store is unavailable instead of "no data" when it is', async () => {
    vi.mocked(api.fetchFleetUsage).mockResolvedValue({
      ...fleetResponse([]),
      blocked_sources: [
        {
          sidecar_id: 'host-a',
          provider_id: 'anthropic',
          credential_origin: 'cookie:anthropic/Chrome/Default/Network/Cookies',
          source_store: 'Chrome/Default/Network/Cookies',
          reason: 'source_locked',
          detail: 'held open by another process',
          remedy: 'paste the cookie in Settings → Providers',
          collecting: false,
        },
      ],
    });
    renderPage();
    expect(await screen.findByText(/cookie store unavailable/i)).toBeInTheDocument();
    expect(
      screen.getByText(/Chrome\/Default\/Network\/Cookies could not be read/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/no data for this provider/i)).not.toBeInTheDocument();
  });

  it('names the origin and no remedy when the server gives nothing else', async () => {
    vi.mocked(api.fetchFleetUsage).mockResolvedValue({
      ...fleetResponse([]),
      blocked_sources: [
        {
          sidecar_id: 'host-a',
          provider_id: 'anthropic',
          credential_origin: 'cookie:anthropic/Chrome/Default/Network/Cookies',
          source_store: null,
          reason: 'source_locked',
          detail: null,
          remedy: null,
          collecting: false,
        },
      ],
    });
    renderPage();
    expect(
      await screen.findByText(
        /cookie:anthropic\/Chrome\/Default\/Network\/Cookies could not be read\./i,
      ),
    ).toBeInTheDocument();
  });

  it('keeps the plain empty state when the blocked source belongs to another provider', async () => {
    vi.mocked(api.fetchFleetUsage).mockResolvedValue({
      ...fleetResponse([]),
      blocked_sources: [
        {
          sidecar_id: 'host-a',
          provider_id: 'ollama',
          credential_origin: 'cookie:ollama/Chrome/Default/Network/Cookies',
          source_store: 'Chrome/Default/Network/Cookies',
          reason: 'source_locked',
          collecting: false,
        },
      ],
    });
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

  it('scrolls the deep-linked tab into view once the tab strip exists', async () => {
    // The strip only renders once the provider's entry has loaded, and the
    // tab comes from the URL, so it never changes again on a cold load. An
    // effect that only watched the tab would have run once, against a strip
    // that was not there yet, and the link would land on a page that looks
    // like the wrong tab is selected.
    const scrollIntoView = vi.fn();
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scrollIntoView;
    try {
      vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([fleetEntry()]));
      renderPage('/provider/anthropic?tab=cost');
      await screen.findByTestId('cost-tab');
      await waitFor(() => expect(scrollIntoView).toHaveBeenCalled());
    } finally {
      Element.prototype.scrollIntoView = original;
    }
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

    await userEvent.click(screen.getByRole('button', { name: /more provider actions/i }));
    await userEvent.click(await screen.findByText('Clear failure state'));
    // Destructive: it asks first and names what it acts on.
    expect(api.resetProvider).not.toHaveBeenCalled();
    await userEvent.click(await screen.findByRole('button', { name: 'Clear failure state' }));
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
    expect(screen.getByRole('combobox', { name: 'Provider account' })).toBeInTheDocument();
  });

  it('selects the account named in the ?account param', async () => {
    const a = fleetEntry({ account_id: 'a@x.com' });
    const b = fleetEntry({ account_id: 'b@x.com' });
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([a, b]));
    renderPage('/provider/anthropic?account=b@x.com');
    expect(await screen.findByTestId('overview-tab')).toHaveTextContent('b@x.com');
  });

  it('shows the time-range picker on period-aware tabs with the default range', async () => {
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([fleetEntry()]));
    renderPage();
    await screen.findByTestId('overview-tab');
    expect(screen.getByRole('button', { name: 'Last 7 days' })).toBeInTheDocument();
  });

  it('honours the legacy ?period deep-link as a month range', async () => {
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([fleetEntry()]));
    renderPage('/provider/anthropic?tab=events&period=2026-03');
    await screen.findByTestId('events-tab');
    expect(screen.getByRole('button', { name: 'March 2026' })).toBeInTheDocument();
  });

  it('applies a quick range and reflects it in the trigger label', async () => {
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([fleetEntry()]));
    renderPage();
    await screen.findByTestId('overview-tab');

    await userEvent.click(screen.getByRole('button', { name: 'Last 7 days' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Last 30 days' }));
    expect(screen.getByRole('button', { name: 'Last 30 days' })).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Last 7 days' }),
    ).not.toBeInTheDocument();
  });

  it('toasts an error when collection fails', async () => {
    const { toast } = await import('sonner');
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([fleetEntry()]));
    vi.mocked(api.collectProvider).mockRejectedValue(new Error('boom'));
    renderPage();
    await screen.findByTestId('overview-tab');

    await userEvent.click(screen.getByRole('button', { name: /collect now/i }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Collect failed: boom'));
  });

  it('toasts an error when reset fails', async () => {
    const { toast } = await import('sonner');
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([fleetEntry()]));
    vi.mocked(api.resetProvider).mockRejectedValue(new Error('nope'));
    renderPage();
    await screen.findByTestId('overview-tab');

    await userEvent.click(screen.getByRole('button', { name: /more provider actions/i }));
    await userEvent.click(await screen.findByText('Clear failure state'));
    await userEvent.click(await screen.findByRole('button', { name: 'Clear failure state' }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Reset failed: nope'));
  });

  it('switches accounts via the selector and reflects it in the URL', async () => {
    const a = fleetEntry({
      account_id: 'a@x.com',
      critical_gauge: limitCard({ account_id: 'a@x.com', account_label: 'Acct A' }),
    });
    const b = fleetEntry({
      account_id: 'b@x.com',
      critical_gauge: limitCard({ account_id: 'b@x.com', account_label: 'Acct B' }),
    });
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([a, b]));
    renderPage();
    // Default falls to the first entry.
    expect(await screen.findByTestId('overview-tab')).toHaveTextContent('a@x.com');

    await userEvent.click(screen.getByRole('combobox', { name: 'Provider account' }));
    await userEvent.click(await screen.findByRole('option', { name: 'Acct B' }));

    expect(await screen.findByTestId('overview-tab')).toHaveTextContent('b@x.com');
  });

  it('collects from the empty-state action button', async () => {
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([]));
    vi.mocked(api.collectProvider).mockResolvedValue({ status: 'ok' } as never);
    renderPage();
    const emptyState = await screen.findByText(/no data for this provider/i);

    // The empty-state CTA renders the visible "Collect now" text (the header
    // button only exposes it via aria-label). Defaults to the 'default'
    // account when there is no fleet entry.
    const cta = emptyState.parentElement!.querySelector('button')!;
    expect(cta).toHaveTextContent(/collect now/i);
    await userEvent.click(cta);
    await waitFor(() =>
      expect(api.collectProvider).toHaveBeenCalledWith('anthropic', 'default'),
    );
  });

  it('falls back to the Overview tab for an unknown ?tab=', async () => {
    // Radix renders a panel only for a matching value, so an unknown tab
    // used to leave the page blank: header, empty tab strip, no content and
    // no way back.
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([fleetEntry()]));
    renderPage('/provider/anthropic?tab=does-not-exist');
    expect(await screen.findByTestId('overview-tab')).toBeInTheDocument();
    // The Overview tab is the selected one.
    expect(screen.getByRole('tab', { name: 'Overview' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
  });

  it('goes to Home when the Back button is clicked', async () => {
    // Back used to be navigate(-1): on a bookmark, a deep link or a refresh
    // there is no in-app history entry, and it left the dashboard entirely.
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([fleetEntry()]));
    renderPage();
    await screen.findByTestId('overview-tab');

    await userEvent.click(screen.getByRole('button', { name: /back/i }));
    expect(await screen.findByTestId('home-route')).toBeInTheDocument();
  });
});
