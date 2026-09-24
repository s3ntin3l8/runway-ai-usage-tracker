import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ApiError } from '@/api/client';
import * as api from '@/api/endpoints';
import { renderWithProviders } from '@/test/utils';
import { PairSidecarPanel } from './PairSidecarPanel';

vi.mock('@/api/endpoints');

const PAIRING = {
  code: 'AB3DE-7XYZ9',
  expires_at: new Date(Date.now() + 10 * 60_000).toISOString(),
  server_url: 'https://runway.example.com',
  deep_link: 'runway-sidecar://pair?server=https%3A%2F%2Frunway.example.com&code=AB3DE-7XYZ9',
};

describe('PairSidecarPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [] });
  });

  it('mints a code for this origin and offers link, code and CLI', async () => {
    vi.mocked(api.createPairingCode).mockResolvedValue(PAIRING);
    renderWithProviders(<PairSidecarPanel />);
    await userEvent.click(screen.getByRole('button', { name: /generate pairing link/i }));

    expect(api.createPairingCode).toHaveBeenCalledWith(window.location.origin);
    const open = await screen.findByRole('link', { name: /open in runway sidecar/i });
    expect(open).toHaveAttribute('href', PAIRING.deep_link);
    expect(screen.getByLabelText('Pairing code')).toHaveTextContent('AB3DE-7XYZ9');
    expect(
      screen.getByText('runway-sidecar-cli --pair https://runway.example.com AB3DE-7XYZ9'),
    ).toBeInTheDocument();
    expect(screen.getByText(/expires in/)).toBeInTheDocument();
    expect(screen.getByText(/waiting for the sidecar/i)).toBeInTheDocument();
  });

  it('announces a sidecar that registers after the code was minted', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [{ sidecar_id: 'old-box' } as never],
    });
    vi.mocked(api.createPairingCode).mockResolvedValue(PAIRING);
    const { client: queryClient } = renderWithProviders(<PairSidecarPanel />);
    await waitFor(() => expect(api.fetchSidecars).toHaveBeenCalled());
    await userEvent.click(screen.getByRole('button', { name: /generate pairing link/i }));
    await screen.findByLabelText('Pairing code');

    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [{ sidecar_id: 'old-box' } as never, { sidecar_id: 'laptop' } as never],
    });
    await queryClient.invalidateQueries({ queryKey: ['fleet', 'sidecars'] });
    expect(await screen.findByText(/laptop connected/)).toBeInTheDocument();
    expect(screen.queryByText(/old-box connected/)).not.toBeInTheDocument();
  });

  it('explains the INGEST_API_KEY requirement on 503', async () => {
    vi.mocked(api.createPairingCode).mockRejectedValue(new ApiError(503, 'disabled'));
    renderWithProviders(<PairSidecarPanel />);
    await userEvent.click(screen.getByRole('button', { name: /generate pairing link/i }));
    expect(await screen.findByText(/INGEST_API_KEY/)).toBeInTheDocument();
  });
});
