import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import * as api from '@/api/endpoints';
import type { ProviderConfig } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { groupProviderFamilies } from '@/lib/providerFamilies';
import { AddProviderWizard } from './AddProviderWizard';
import { ConsoleOrganizationPicker } from './ConsoleOrganizationPicker';
import { ProvidersSection } from './ProvidersSection';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
const orgId = '11111111-1111-4111-8111-111111111111';
const quota: ProviderConfig = { provider_id: 'anthropic', name: 'Claude', accounts: [], account_count: 0, supports_api_key: true };
const consoleProvider: ProviderConfig = {
  provider_id: 'anthropic_console', name: 'Claude · Console balance', accounts: [], account_count: 0,
  supports_session_cookie: true, session_cookie_label: 'Console session cookie', supported_strategies: [{ id: 'web', enabled: true }],
};

beforeEach(() => vi.clearAllMocks());

it('groups Claude connections without changing their original configurations', () => {
  const grouped = groupProviderFamilies([quota, consoleProvider]);
  expect(grouped).toHaveLength(1);
  expect(grouped[0].provider_id).toBe('anthropic');
  expect(consoleProvider.provider_id).toBe('anthropic_console');
});

it('selects Console under Claude, verifies an organization, and saves a PAYG connection', async () => {
  vi.mocked(api.discoverConsoleOrganizations).mockResolvedValue({ organizations: [{ id: orgId, name: 'Individual Org' }] });
  vi.mocked(api.previewAccount).mockResolvedValue({
    suggested_account_id: orgId, suggested_label: 'Individual Org', label_source: 'organization', already_exists: false,
  });
  vi.mocked(api.putProviderConfig).mockResolvedValue({ status: 'saved', account_id: orgId });
  renderWithProviders(<AddProviderWizard providers={[quota, consoleProvider]} existingAccountIdsByProvider={new Map()} onClose={vi.fn()} />);
  await userEvent.click(screen.getByRole('button', { name: /Claude/ }));
  await userEvent.selectOptions(screen.getByLabelText('Claude connection'), 'anthropic_console');
  const cookie = screen.getByLabelText('Console session cookie');
  expect(cookie).toHaveAttribute('type', 'password');
  await userEvent.type(cookie, 'sample-session');
  expect(api.previewAccount).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole('button', { name: 'Load Console organizations' }));
  await waitFor(() => expect(screen.getByLabelText('API organization')).toHaveValue(orgId));
  await waitFor(() => expect(api.previewAccount).toHaveBeenCalledWith(expect.objectContaining({
    provider_id: 'anthropic_console', session_cookie: 'sample-session', organization_id: orgId,
  }), expect.any(AbortSignal)));
  await waitFor(() => expect(screen.getByRole('button', { name: /Next/ })).toBeEnabled());
  await userEvent.click(screen.getByRole('button', { name: /Next/ }));
  fireEvent.change(screen.getByLabelText('Account label'), { target: { value: 'alias@example.com' } });
  await userEvent.click(screen.getByRole('button', { name: /^Save/ }));
  await waitFor(() => expect(api.putProviderConfig).toHaveBeenCalledWith('anthropic_console', orgId, expect.objectContaining({
    session_cookie: 'sample-session', billing_type: 'pay_as_you_go', account_label: 'alias@example.com',
  })));
});

it('requires an explicit choice when multiple organizations are accessible', async () => {
  vi.mocked(api.discoverConsoleOrganizations).mockResolvedValue({ organizations: [{ id: orgId, name: 'One' }, { id: 'other', name: 'Two' }] });
  const onSelect = vi.fn();
  renderWithProviders(<ConsoleOrganizationPicker cookie="sample-session" value="" onSelect={onSelect} />);
  await userEvent.click(screen.getByRole('button', { name: 'Load Console organizations' }));
  await screen.findByRole('option', { name: 'One' });
  expect(onSelect).toHaveBeenCalledWith('');
  await userEvent.selectOptions(screen.getByLabelText('API organization'), orgId);
  expect(onSelect).toHaveBeenLastCalledWith(orgId);
});

it('shows discovery errors and empty organization lists', async () => {
  vi.mocked(api.discoverConsoleOrganizations).mockRejectedValueOnce(new Error('Session expired')).mockResolvedValueOnce({ organizations: [] });
  renderWithProviders(<ConsoleOrganizationPicker cookie="sample-session" value="" onSelect={vi.fn()} />);
  await userEvent.click(screen.getByRole('button', { name: 'Load Console organizations' }));
  await screen.findByText('Session expired');
  await userEvent.click(screen.getByRole('button', { name: 'Load Console organizations' }));
  await screen.findByText(/No API organizations/);
});

it('discards stale discovery results when the cookie changes', async () => {
  let resolve: (value: { organizations: { id: string; name: string }[] }) => void = () => {};
  vi.mocked(api.discoverConsoleOrganizations).mockImplementation(() => new Promise((done) => { resolve = done; }));
  const onSelect = vi.fn();
  const view = renderWithProviders(<ConsoleOrganizationPicker cookie="old-session" value="" onSelect={onSelect} />);
  await userEvent.click(screen.getByRole('button', { name: 'Load Console organizations' }));
  view.rerender(<ConsoleOrganizationPicker cookie="new-session" value="" onSelect={onSelect} />);
  await act(async () => resolve({ organizations: [{ id: orgId, name: 'Stale organization' }] }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Load Console organizations' })).toBeEnabled());
  expect(onSelect).not.toHaveBeenCalled();
});

it('shows one Claude settings entry and switches to independent Console account settings', async () => {
  const configuredQuota = { ...quota, accounts: [{ account_id: 'alice@example.com', account_label: 'Subscription login', enabled: true }], account_count: 1 };
  const configuredConsole = { ...consoleProvider, accounts: [{ account_id: orgId, account_label: 'Individual Org', enabled: true, session_cookie_set: true }], account_count: 1 };
  vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [configuredQuota, configuredConsole] });
  vi.mocked(api.getDashboardLayout).mockResolvedValue({ provider_order: ['anthropic_console', 'anthropic'], card_orders: {} });
  vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({ items: [], counts_by_sidecar: {} });
  vi.mocked(api.fetchCredentialInventory).mockResolvedValue({ providers: [], machines: [], unmapped_count: 0, rule_count: 0, pending_usage_events: 0 });
  renderWithProviders(<ProvidersSection />);
  const open = await screen.findByRole('button', { name: /Claude 2 accounts/ });
  await userEvent.click(open);
  await userEvent.selectOptions(screen.getByLabelText('Claude connection'), 'anthropic_console');
  const dialog = screen.getByRole('dialog');
  expect(within(dialog).getByText('Individual Org')).toBeInTheDocument();
  expect(within(dialog).queryByText('Subscription login')).not.toBeInTheDocument();
});
