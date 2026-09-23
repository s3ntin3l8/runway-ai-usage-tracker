import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders } from '@/test/utils';
import type { ArchivedProvider } from '@/api/endpoints';
import { ArchivedCard } from './ArchivedCard';

const mockNavigate = vi.fn();

vi.mock('react-router', async () => {
  const actual = await vi.importActual('react-router');
  return { ...actual, useNavigate: () => mockNavigate };
});

const makeItem = (overrides: Partial<ArchivedProvider> = {}): ArchivedProvider => ({
  provider_id: 'anthropic',
  account_id: 'u@example.com',
  lifetime: {
    tokens_input: 1000,
    tokens_output: 2000,
    tokens_cache_read: 500,
    tokens_cache_create: 200,
    tokens_reasoning: 100,
    msgs: 42,
    cost_usd: 5.67,
    by_model: {},
  },
  last_activity_ts: '2026-03-15T10:00:00Z',
  ...overrides,
});

describe('ArchivedCard', () => {
  beforeEach(() => {
    mockNavigate.mockClear();
  });

  it('renders provider name and archived badge', () => {
    renderWithProviders(<ArchivedCard item={makeItem()} providerName="Claude" />);
    expect(screen.getByText('Claude')).toBeInTheDocument();
    expect(screen.getByText('archived')).toBeInTheDocument();
  });

  it('matches the normal provider card min height', () => {
    renderWithProviders(<ArchivedCard item={makeItem()} providerName="Claude" />);
    const cardEl = screen.getByRole('button');
    expect(cardEl.className).toContain('min-h-36');
    expect(cardEl.className).toContain('flex-col');
  });

  it('shows lifetime stats when lifetime is present', () => {
    renderWithProviders(<ArchivedCard item={makeItem()} providerName="Claude" />);
    expect(screen.getByText('Tokens')).toBeInTheDocument();
    expect(screen.getByText('Msgs')).toBeInTheDocument();
    expect(screen.getByText('Cost')).toBeInTheDocument();
  });

  it('shows "No usage data" when lifetime is null', () => {
    renderWithProviders(<ArchivedCard item={makeItem({ lifetime: null })} providerName="Claude" />);
    expect(screen.getByText('No usage data')).toBeInTheDocument();
  });

  it('shows account_id when not default', () => {
    renderWithProviders(<ArchivedCard item={makeItem({ account_id: 'alt-account' })} providerName="Claude" />);
    expect(screen.getByText('alt-account')).toBeInTheDocument();
  });

  it('hides account_id when default', () => {
    renderWithProviders(<ArchivedCard item={makeItem({ account_id: 'default' })} providerName="Claude" />);
    expect(screen.queryByText('default')).not.toBeInTheDocument();
  });

  it('shows last active text when last_activity_ts is present', () => {
    renderWithProviders(<ArchivedCard item={makeItem()} providerName="Claude" />);
    expect(screen.getByText(/Last active/)).toBeInTheDocument();
  });

  it('hides last active when last_activity_ts is null', () => {
    renderWithProviders(<ArchivedCard item={makeItem({ last_activity_ts: null })} providerName="Claude" />);
    expect(screen.queryByText(/Last active/)).not.toBeInTheDocument();
  });

  it('navigates on click', async () => {
    const user = userEvent.setup();
    renderWithProviders(<ArchivedCard item={makeItem()} providerName="Claude" />);
    await user.click(screen.getByRole('button'));
    expect(mockNavigate).toHaveBeenCalledWith('/provider/anthropic?account=u@example.com');
  });

  it('navigates on Enter key', async () => {
    const user = userEvent.setup();
    renderWithProviders(<ArchivedCard item={makeItem()} providerName="Claude" />);
    await user.tab();
    await user.keyboard('{Enter}');
    expect(mockNavigate).toHaveBeenCalledWith('/provider/anthropic?account=u@example.com');
  });

  it('navigates on Space key', async () => {
    const user = userEvent.setup();
    renderWithProviders(<ArchivedCard item={makeItem()} providerName="Claude" />);
    await user.tab();
    await user.keyboard(' ');
    expect(mockNavigate).toHaveBeenCalledWith('/provider/anthropic?account=u@example.com');
  });
});
