import { screen } from '@testing-library/react';
import * as api from '@/api/endpoints';
import type { BlockedSource } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { UnavailableSourcesView } from './UnavailableSourcesView';

vi.mock('@/api/endpoints');

const DEVICE = `device-${'b'.repeat(32)}`;
const source = (sidecar_id: string): BlockedSource =>
  ({
    sidecar_id,
    provider_id: 'anthropic',
    credential_origin: 'cookie:anthropic/Chrome/Default/Cookies',
    reason: 'source_locked',
    first_seen: '2026-10-28T00:00:00Z',
  }) as BlockedSource;

describe('UnavailableSourcesView', () => {
  beforeEach(() => vi.clearAllMocks());

  it('says which machine by name, not by its device id', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [{ sidecar_id: DEVICE, hostname: 'macbook' }],
    } as never);
    renderWithProviders(<UnavailableSourcesView sources={[source(DEVICE)]} />);

    expect(await screen.findByText(/^on macbook/)).toBeInTheDocument();
    expect(screen.queryByText(new RegExp(DEVICE))).not.toBeInTheDocument();
  });

  it('keeps the raw id for a machine the registry does not know', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [] } as never);
    renderWithProviders(<UnavailableSourcesView sources={[source('retired-host')]} />);

    expect(await screen.findByText(/^on retired-host/)).toBeInTheDocument();
  });
});
