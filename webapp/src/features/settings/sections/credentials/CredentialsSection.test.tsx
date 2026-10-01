import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import * as api from '@/api/endpoints';
import { renderWithProviders } from '@/test/utils';
import { CredentialsSection } from './CredentialsSection';
import { inventory, multiMachineInventory } from './testData';

vi.mock('@/api/endpoints');
vi.mock('@/features/fleet/PendingUsageEventsCard', () => ({
  PendingUsageEventsCard: () => <div>Pending usage card</div>,
}));

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [] });
  vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({ items: [], counts_by_sidecar: {} });
  vi.mocked(api.fetchCredentialTags).mockResolvedValue({ items: [] });
  vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [] });
});

describe('CredentialsSection', () => {
  it('opens on the by-provider view', async () => {
    vi.mocked(api.fetchCredentialInventory).mockResolvedValue(multiMachineInventory());
    renderWithProviders(<CredentialsSection />);
    expect(await screen.findByRole('heading', { name: 'Gemini' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /by provider/i })).toHaveAttribute('data-state', 'active');
  });

  it('switches views and keeps the choice in the URL', async () => {
    vi.mocked(api.fetchCredentialInventory).mockResolvedValue(multiMachineInventory());
    renderWithProviders(<CredentialsSection />);
    await screen.findByRole('heading', { name: 'Gemini' });

    await userEvent.click(screen.getByRole('tab', { name: /by machine/i }));
    expect(await screen.findByRole('heading', { name: 'DEV-01' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /by machine/i })).toHaveAttribute('data-state', 'active');
  });

  it.each([
    ['machine', 'tab', /by machine/i],
    ['mapping', 'tab', /needs mapping/i],
    ['rules', 'tab', /^rules/i],
  ])('deep-links to the %s view', async (view, role, name) => {
    vi.mocked(api.fetchCredentialInventory).mockResolvedValue(multiMachineInventory());
    renderWithProviders(<CredentialsSection />, { route: `/settings/credentials?view=${view}` });
    await waitFor(() =>
      expect(screen.getByRole(role, { name })).toHaveAttribute('data-state', 'active'),
    );
  });

  it('falls back to by-provider for an unknown view', async () => {
    vi.mocked(api.fetchCredentialInventory).mockResolvedValue(multiMachineInventory());
    renderWithProviders(<CredentialsSection />, { route: '/settings/credentials?view=bogus' });
    expect(await screen.findByRole('heading', { name: 'Gemini' })).toBeInTheDocument();
  });

  it('badges the mapping and rules tabs with what is waiting', async () => {
    vi.mocked(api.fetchCredentialInventory).mockResolvedValue(
      inventory({ unmapped_count: 2, pending_usage_events: 3, rule_count: 4 }),
    );
    renderWithProviders(<CredentialsSection />);
    expect(await screen.findByRole('tab', { name: /needs mapping\s*5/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /rules\s*4/i })).toBeInTheDocument();
  });

  it('explains an empty inventory', async () => {
    vi.mocked(api.fetchCredentialInventory).mockResolvedValue(inventory());
    renderWithProviders(<CredentialsSection />);
    expect(await screen.findByText('No credentials found yet')).toBeInTheDocument();
  });

  it('explains when there are no machines', async () => {
    vi.mocked(api.fetchCredentialInventory).mockResolvedValue(inventory());
    renderWithProviders(<CredentialsSection />, { route: '/settings/credentials?view=machine' });
    expect(await screen.findByText('No machines yet')).toBeInTheDocument();
  });

  it('shows a load error', async () => {
    vi.mocked(api.fetchCredentialInventory).mockRejectedValue(new Error('server down'));
    renderWithProviders(<CredentialsSection />);
    expect(await screen.findByText(/Couldn't load credentials: server down/)).toBeInTheDocument();
  });
});
