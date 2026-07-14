import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { FleetEntry, LimitCard } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { ProviderGrid } from './ProviderGrid';
import { AtRiskRail } from './AtRiskRail';
import { buildRiskItems, atRiskItems } from './risk';
import type { ForecastEntry } from '@/api/types';

const card = (o: Partial<LimitCard> = {}): LimitCard => ({
  service_name: 'Claude',
  pct_used: 50,
  window_type: 'weekly',
  reset_at: new Date(Date.now() + 3_600_000).toISOString(),
  updated_at: new Date().toISOString(),
  ...o,
});

const entry = (o: Partial<FleetEntry> = {}): FleetEntry => ({
  provider_id: 'claude',
  account_id: 'default',
  critical_gauge: card(),
  secondary_limits: [],
  ...o,
});

const names = new Map([['claude', 'Claude']]);

describe('ProviderGrid', () => {
  it('renders a card per item with provider name and percentage', () => {
    const items = buildRiskItems([entry({ critical_gauge: card({ tier: 'Pro' }) })], []);
    renderWithProviders(<ProviderGrid items={items} providerNames={names} onReorder={vi.fn()} />);
    expect(screen.getByText('Providers')).toBeInTheDocument();
    expect(screen.getByText('Claude')).toBeInTheDocument();
    expect(screen.getByText('Pro')).toBeInTheDocument(); // tier badge
  });

  it('renders secondary-limit chips with overflow', () => {
    const secondaries = [
      card({ service_name: 'session', window_type: 'session', pct_used: 10 }),
      card({ service_name: 'daily', window_type: 'daily', pct_used: 20 }),
      card({ service_name: 'monthly', window_type: 'monthly', pct_used: 30 }),
      card({ service_name: 'extra', window_type: 'rolling', pct_used: 40 }),
    ];
    const items = buildRiskItems([entry({ secondary_limits: secondaries })], []);
    renderWithProviders(<ProviderGrid items={items} providerNames={names} onReorder={vi.fn()} />);
    // 4 secondaries, only 3 shown → "+1" overflow badge.
    expect(screen.getByText('+1')).toBeInTheDocument();
  });

  it('navigates to the provider detail when a card is clicked', async () => {
    const items = buildRiskItems([entry()], []);
    renderWithProviders(<ProviderGrid items={items} providerNames={names} onReorder={vi.fn()} />);
    const cardEl = screen.getByRole('button', { name: /claude.*used/i });
    await userEvent.click(cardEl);
    // No assertion target beyond not throwing — navigate is internal to the
    // MemoryRouter. Exercise the keyboard path too.
    cardEl.focus();
    await userEvent.keyboard('{Enter}');
  });

  it('falls back to the provider_id when no friendly name is known', () => {
    const items = buildRiskItems([entry({ provider_id: 'mystery' })], []);
    renderWithProviders(
      <ProviderGrid items={items} providerNames={new Map()} onReorder={vi.fn()} />,
    );
    expect(screen.getByText('mystery')).toBeInTheDocument();
  });

  it('shows account_label when the card has one', () => {
    const items = buildRiskItems(
      [entry({ account_id: 'user@example.com', critical_gauge: card({ account_label: 'Work' }) })],
      [],
    );
    renderWithProviders(<ProviderGrid items={items} providerNames={names} onReorder={vi.fn()} />);
    expect(screen.getByText('Work')).toBeInTheDocument();
  });

  it('falls back to account_id when no account_label but id is non-default', () => {
    const items = buildRiskItems(
      [entry({ account_id: 'alice@example.com', critical_gauge: card({ account_label: null }) })],
      [],
    );
    renderWithProviders(<ProviderGrid items={items} providerNames={names} onReorder={vi.fn()} />);
    expect(screen.getByText('alice@example.com')).toBeInTheDocument();
  });

  it('does not show an account identity for a single default account', () => {
    // account_id='default' with no label → no secondary text cluttering the card.
    const items = buildRiskItems(
      [entry({ account_id: 'default', critical_gauge: card({ account_label: null }) })],
      [],
    );
    renderWithProviders(<ProviderGrid items={items} providerNames={names} onReorder={vi.fn()} />);
    // 'default' should not appear as visible text in the card.
    expect(screen.queryByText('default')).not.toBeInTheDocument();
  });

  it('respects the exclude-cache toggle in the tokens-kind hero metric', () => {
    const tokenCard = card({
      pct_used: undefined,
      is_unlimited: true,
      token_usage: { input: 100, output: 50, reasoning: 10, cache_read: 700, cache_create: 140 },
    });
    const items = buildRiskItems([entry({ critical_gauge: tokenCard })], []);

    localStorage.setItem('runway_exclude_cache', '0');
    const { unmount } = renderWithProviders(
      <ProviderGrid items={items} providerNames={names} onReorder={vi.fn()} />,
    );
    expect(screen.getByText('1K')).toBeInTheDocument();
    unmount();

    localStorage.setItem('runway_exclude_cache', '1');
    renderWithProviders(<ProviderGrid items={items} providerNames={names} onReorder={vi.fn()} />);
    expect(screen.getByText('160')).toBeInTheDocument();
  });
});

describe('AtRiskRail', () => {
  const forecast = (o: Partial<ForecastEntry> = {}): ForecastEntry => ({
    provider_id: 'claude',
    account_id: 'default',
    status: 'risk',
    // window_type must match the card so findForecast can associate them.
    window_type: 'weekly',
    now_pct: 80,
    projected_pct: 110,
    slope: 0.01,
    glide_pct: 90,
    ...o,
  });

  it('shows the all-clear row when no item is at risk', () => {
    renderWithProviders(<AtRiskRail items={[]} providerNames={names} />);
    expect(screen.getByText(/all clear/i)).toBeInTheDocument();
  });

  it('renders an at-risk card with the forecast badge', () => {
    const items = buildRiskItems(
      [entry({ critical_gauge: card({ pct_used: 95 }) })],
      [forecast()],
    );
    const rail = atRiskItems(items);
    renderWithProviders(<AtRiskRail items={rail} providerNames={names} />);
    expect(screen.getByText('At risk')).toBeInTheDocument();
    // forecastLabel renders a "limit in ~Xh"/"projected"/etc string.
    expect(screen.getByText(/limit in|projected|exhausted/i)).toBeInTheDocument();
  });

  it('flags a collector error on the at-risk card', () => {
    const items = buildRiskItems(
      [entry({ critical_gauge: card({ error_type: 'auth', pct_used: null }) })],
      [],
    );
    const rail = atRiskItems(items);
    renderWithProviders(<AtRiskRail items={rail} providerNames={names} />);
    expect(screen.getByText(/collector error/i)).toBeInTheDocument();
  });
});
