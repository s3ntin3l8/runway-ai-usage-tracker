import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import * as api from '@/api/endpoints';
import { useCredentialInventory } from './queries';

vi.mock('@/api/endpoints');

describe('useCredentialInventory', () => {
  it('does not retry a failing (e.g. 403 for a non-admin) request', async () => {
    vi.mocked(api.fetchCredentialInventory).mockRejectedValue(new Error('403'));
    // Library defaults (3 retries) rather than the test client's retry:false, so this only
    // passes if the hook itself opts out of retrying.
    const client = new QueryClient({ defaultOptions: { queries: { retry: 3, retryDelay: 0 } } });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );

    const { result } = renderHook(() => useCredentialInventory(), { wrapper });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(api.fetchCredentialInventory).toHaveBeenCalledTimes(1);
  });
});
