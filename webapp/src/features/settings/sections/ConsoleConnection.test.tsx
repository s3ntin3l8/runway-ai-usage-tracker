import { useState } from 'react';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import * as api from '@/api/endpoints';
import type { ProviderConfig } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { groupProviderFamilies } from '@/lib/providerFamilies';
import { AddProviderWizard } from './AddProviderWizard';
import { foreignError } from '@/test/foreignError';
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
    suggested_account_id: orgId, suggested_label: 'Individual Org', label_source: 'organization',
    suggested_billing_type: 'pay_as_you_go', already_exists: false,
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
  // The wizard does not hardcode a billing model: the server proves it from
  // the balance the account publishes, and the select shows what it proved.
  expect(screen.getByLabelText('Billing model')).toHaveValue('pay_as_you_go');
  await waitFor(() => expect(api.putProviderConfig).toHaveBeenCalledWith('anthropic_console', orgId, expect.objectContaining({
    session_cookie: 'sample-session', account_label: 'alias@example.com',
  })));
  expect(vi.mocked(api.putProviderConfig).mock.calls[0][2]).not.toHaveProperty('billing_type');
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
  vi.mocked(api.discoverConsoleOrganizations).mockRejectedValueOnce(foreignError('Session expired')).mockResolvedValueOnce({ organizations: [] });
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
  expect(onSelect).toHaveBeenCalledExactlyOnceWith('');
});

it('does not report an error for a request it aborted', async () => {
  let reject: (reason: unknown) => void = () => {};
  vi.mocked(api.discoverConsoleOrganizations)
    .mockImplementationOnce(() => new Promise((_done, fail) => { reject = fail; }));
  const onSelect = vi.fn();
  const view = renderWithProviders(<ConsoleOrganizationPicker cookie="old-session" value="" onSelect={onSelect} />);
  await userEvent.click(screen.getByRole('button', { name: 'Load Console organizations' }));
  // A new cookie aborts the request that is in flight.
  view.rerender(<ConsoleOrganizationPicker cookie="new-session" value="" onSelect={onSelect} />);
  await act(async () => { reject(foreignError('Session expired')); });
  // The aborted request failed anyway; its error must not be shown, because
  // the component has already moved on to the new cookie.
  expect(screen.queryByText('Session expired')).not.toBeInTheDocument();
});

it('shows one Claude settings entry and switches to independent Console account settings', async () => {
  const configuredQuota = { ...quota, accounts: [{ account_id: 'alice@example.com', account_label: 'Subscription login', enabled: true }], account_count: 1 };
  const configuredConsole = { ...consoleProvider, accounts: [{ account_id: orgId, account_label: 'Individual Org', enabled: false, session_cookie_set: true }], account_count: 1 };
  vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [configuredQuota, configuredConsole] });
  vi.mocked(api.getDashboardLayout).mockResolvedValue({ provider_order: ['anthropic_console', 'anthropic'], card_orders: {} });
  vi.mocked(api.fetchUntaggedCredentials).mockResolvedValue({ items: [], counts_by_sidecar: {} });
  vi.mocked(api.fetchCredentialInventory).mockResolvedValue({ providers: [], machines: [], unmapped_count: 0, rule_count: 0, pending_usage_events: 0 });
  renderWithProviders(<ProvidersSection />);
  const open = await screen.findByRole('button', { name: /Claude 2 accounts/ });
  expect(within(open).getByText('connections: partial')).toBeInTheDocument();
  await userEvent.click(open);
  await userEvent.selectOptions(screen.getByLabelText('Claude connection'), 'anthropic_console');
  const dialog = screen.getByRole('dialog');
  expect(within(dialog).getByText('Individual Org')).toBeInTheDocument();
  // The card counts both connections' accounts, so the dialog lists them both
  // — each row says which connection it belongs to (#723). The connection
  // dropdown picks whose *settings* are shown, not whose accounts are listed.
  expect(within(dialog).getByText('Subscription login')).toBeInTheDocument();
  const list = within(dialog).getByLabelText('Provider accounts');
  expect(within(list).getByText('Subscription quotas')).toBeInTheDocument();
  expect(within(list).getByText('Console API credits')).toBeInTheDocument();
});

function StatefulPicker({ cookie = 'sample-session', initial = orgId }: { cookie?: string; initial?: string }) {
  const [selected, setSelected] = useState(initial);
  return <>
    <ConsoleOrganizationPicker cookie={cookie} value={selected} onSelect={setSelected} />
    <output aria-label="Selected organization">{selected}</output>
  </>;
}

const multipleOrgs = [{ id: orgId, name: 'One' }, { id: 'other', name: 'Two' }];

it('preserves an explicit organization choice when reloading with the same cookie', async () => {
  vi.mocked(api.discoverConsoleOrganizations).mockResolvedValue({ organizations: multipleOrgs });
  renderWithProviders(<StatefulPicker initial="" />);
  await userEvent.click(screen.getByRole('button', { name: 'Load Console organizations' }));
  await screen.findByRole('option', { name: 'One' });
  await userEvent.selectOptions(screen.getByLabelText('API organization'), orgId);
  await userEvent.click(screen.getByRole('button', { name: 'Load Console organizations' }));
  await waitFor(() => expect(api.discoverConsoleOrganizations).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Load Console organizations' })).toBeEnabled());
  expect(screen.getByLabelText('API organization')).toHaveValue(orgId);
  expect(screen.getByLabelText('Selected organization')).toHaveTextContent(orgId);
  expect(screen.queryByText(/Previously selected organization/)).not.toBeInTheDocument();
});

it.each([
  { organizations: [{ id: 'other', name: 'Two' }, { id: 'third', name: 'Three' }] },
  { organizations: [] },
])('clears the selection when the selected organization is absent after refresh (%j)', async ({ organizations }) => {
  vi.mocked(api.discoverConsoleOrganizations)
    .mockResolvedValueOnce({ organizations: multipleOrgs })
    .mockResolvedValueOnce({ organizations });
  renderWithProviders(<StatefulPicker />);
  await userEvent.click(screen.getByRole('button', { name: 'Load Console organizations' }));
  await screen.findByRole('option', { name: 'One' });
  await userEvent.click(screen.getByRole('button', { name: 'Load Console organizations' }));
  await waitFor(() => expect(screen.getByLabelText('Selected organization')).toBeEmptyDOMElement());
  expect(screen.getByText('Previously selected organization is no longer accessible.')).toBeInTheDocument();
  if (organizations.length > 0) {
    await userEvent.selectOptions(screen.getByLabelText('API organization'), 'other');
    expect(screen.queryByText(/Previously selected organization/)).not.toBeInTheDocument();
  }
});

it('clears a selected organization when the cookie changes', async () => {
  vi.mocked(api.discoverConsoleOrganizations).mockResolvedValue({ organizations: multipleOrgs });
  const view = renderWithProviders(<StatefulPicker cookie="old-session" />);
  await userEvent.click(screen.getByRole('button', { name: 'Load Console organizations' }));
  await screen.findByRole('option', { name: 'One' });
  view.rerender(<StatefulPicker cookie="new-session" />);
  await waitFor(() => expect(screen.getByLabelText('Selected organization')).toBeEmptyDOMElement());
  expect(screen.queryByLabelText('API organization')).not.toBeInTheDocument();
});

it('preserves a choice made while a refresh is in flight', async () => {
  let resolve: (value: { organizations: typeof multipleOrgs }) => void = () => {};
  vi.mocked(api.discoverConsoleOrganizations)
    .mockResolvedValueOnce({ organizations: multipleOrgs })
    .mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
  renderWithProviders(<StatefulPicker />);
  await userEvent.click(screen.getByRole('button', { name: 'Load Console organizations' }));
  await screen.findByRole('option', { name: 'One' });
  await userEvent.click(screen.getByRole('button', { name: 'Load Console organizations' }));
  await userEvent.selectOptions(screen.getByLabelText('API organization'), 'other');
  await act(async () => resolve({ organizations: multipleOrgs }));
  expect(screen.getByLabelText('API organization')).toHaveValue('other');
  expect(screen.getByLabelText('Selected organization')).toHaveTextContent('other');
});


it('auto-selects the sole remaining organization and explains a removed selection', async () => {
  vi.mocked(api.discoverConsoleOrganizations)
    .mockResolvedValueOnce({ organizations: multipleOrgs })
    .mockResolvedValueOnce({ organizations: [{ id: 'other', name: 'Two' }] });
  const view = renderWithProviders(<StatefulPicker cookie="sample-session" />);
  await userEvent.click(screen.getByRole('button', { name: 'Load Console organizations' }));
  await screen.findByRole('option', { name: 'One' });
  await userEvent.click(screen.getByRole('button', { name: 'Load Console organizations' }));
  await waitFor(() => expect(screen.getByLabelText('API organization')).toHaveValue('other'));
  expect(screen.getByText('Previously selected organization is no longer accessible.')).toBeInTheDocument();
  view.rerender(<StatefulPicker cookie="replacement-session" />);
  expect(screen.queryByText(/Previously selected organization/)).not.toBeInTheDocument();
});
