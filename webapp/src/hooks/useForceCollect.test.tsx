import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { toast } from 'sonner';
import * as api from '@/api/endpoints';
import { useForceCollect } from './useForceCollect';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

function wrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

describe('useForceCollect', () => {
  let client: QueryClient;

  beforeEach(() => {
    vi.clearAllMocks();
    client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    vi.spyOn(client, 'invalidateQueries');
  });

  it('toasts success and invalidates usage/fleet/token-health on a successful collect', async () => {
    vi.mocked(api.forceCollect).mockResolvedValue({ ok: true, cards: 3, sidecars_triggered: 1 });
    const { result } = renderHook(() => useForceCollect(), { wrapper: wrapper(client) });

    await act(async () => {
      await result.current.mutateAsync();
    });

    expect(toast.success).toHaveBeenCalledWith('Collection triggered — 3 cards, 1 sidecars');
    expect(client.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['usage'] });
    expect(client.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['fleet'] });
    expect(client.invalidateQueries).toHaveBeenCalledWith({
      queryKey: ['system', 'token-health'],
    });
  });

  it('toasts an error and does not invalidate anything when the collect fails', async () => {
    vi.mocked(api.forceCollect).mockRejectedValue(new Error('unauthorized'));
    const { result } = renderHook(() => useForceCollect(), { wrapper: wrapper(client) });

    await act(async () => {
      await result.current.mutateAsync().catch(() => {});
    });

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Collection failed: unauthorized'));
    expect(client.invalidateQueries).not.toHaveBeenCalled();
  });
});
