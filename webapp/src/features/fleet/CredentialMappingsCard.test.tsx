import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import * as api from '@/api/endpoints';
import { renderWithProviders } from '@/test/utils';
import { CredentialMappingsCard } from './CredentialMappingsCard';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const scoped = {
  provider_id: 'anthropic',
  credential_origin: 'provider:anthropic',
  account_id: 'alice@example.com',
  sidecar_id: 'laptop',
  set_by: 'operator',
  set_at: '2026-09-24T10:00:00+00:00',
};
const deployment = { ...scoped, account_id: 'team@example.com', sidecar_id: null };

describe('CredentialMappingsCard', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders nothing when there are no tags', async () => {
    vi.mocked(api.fetchCredentialTags).mockResolvedValue({ items: [] });
    const { container } = renderWithProviders(<CredentialMappingsCard />);
    await waitFor(() => expect(api.fetchCredentialTags).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it('lists machine-scoped and all-machines tags', async () => {
    vi.mocked(api.fetchCredentialTags).mockResolvedValue({ items: [scoped, deployment] });
    renderWithProviders(<CredentialMappingsCard />);
    expect(await screen.findByText('Credential mappings')).toBeInTheDocument();
    expect(screen.getByText('laptop')).toBeInTheDocument();
    expect(screen.getByText('all machines')).toBeInTheDocument();
  });

  it('removes exactly the clicked scope', async () => {
    vi.mocked(api.fetchCredentialTags).mockResolvedValue({ items: [scoped, deployment] });
    vi.mocked(api.deleteCredentialTag).mockResolvedValue({ status: 'deleted' });
    renderWithProviders(<CredentialMappingsCard />);
    await userEvent.click(
      await screen.findByRole('button', {
        name: 'Remove mapping anthropic/provider:anthropic/*',
      }),
    );
    await waitFor(() => expect(api.deleteCredentialTag).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.deleteCredentialTag).mock.calls[0][0]).toMatchObject({
      sidecar_id: null,
      account_id: 'team@example.com',
    });
  });
});
