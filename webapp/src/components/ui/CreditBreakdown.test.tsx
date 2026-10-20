import { screen } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';
import { CreditBreakdown } from './CreditBreakdown';
import { ProviderGrid } from '@/features/home/ProviderGrid';
import { buildRiskItems } from '@/features/home/risk';
import type { CreditBalanceComponent, LimitCard } from '@/api/types';

const entries: CreditBalanceComponent[] = [
  { kind: 'purchased', label: 'Purchased credits', currency: 'USD', remaining_value: 40, granted_value: 50, products: [] },
  { kind: 'subscription', label: 'Subscription API credits', currency: 'USD', remaining_value: 75, granted_value: 100, expires_at: '2030-01-16T00:00:00Z', products: ['api', 'agent_sdk'] },
];

it('shows grant usage and expiry without calling it a reset or next-credit date', () => {
  renderWithProviders(<CreditBreakdown entries={entries} />);
  expect(screen.getByText('$25.00 of $100.00 used')).toBeInTheDocument();
  expect(screen.getByText('$40.00 remaining')).toBeInTheDocument();
  expect(screen.getByText(/Expires/)).toBeInTheDocument();
  expect(screen.queryByText(/resets|next credit/i)).not.toBeInTheDocument();
  expect(screen.getByText(/Applies to/)).toBeInTheDocument();
});

it('keeps the existing PAYG balance hero even when a grant has a usage percentage', () => {
  const gauge: LimitCard = { service_name: 'Claude Console', remaining: '$115.00', unit: 'remaining', unit_type: 'currency', currency: 'USD', health: 'good', credit_breakdown: entries };
  const items = buildRiskItems([{ provider_id: 'anthropic_console', account_id: 'org', billing_type: 'pay_as_you_go', critical_gauge: gauge, secondary_limits: [] }], []);
  renderWithProviders(<ProviderGrid items={items} providerNames={new Map([['anthropic_console', 'Claude Console']])} onReorder={vi.fn()} />);
  expect(screen.getByText('$115.00')).toBeInTheDocument();
  expect(screen.getByText('$25.00 of $100.00 used')).toBeInTheDocument();
  expect(screen.queryByText('25%')).not.toBeInTheDocument();
});

it('hides product restrictions in compact mode and supports grants without a known original amount', () => {
  renderWithProviders(<CreditBreakdown compact entries={[{ ...entries[1], granted_value: null }]} />);
  expect(screen.queryByText(/Applies to|of .*used/)).not.toBeInTheDocument();
  expect(screen.getByText('$75.00 remaining')).toBeInTheDocument();
});
