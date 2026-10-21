import type { ReactNode } from 'react';
import { QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import * as api from '@/api/endpoints';
import { makeQueryClient } from '@/test/utils';
import { useProviderSourceApps } from './queries';

vi.mock('@/api/endpoints');

function wrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={makeQueryClient()}>
      <MemoryRouter initialEntries={['/']}>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

const empty = {
  apps: [],
  metric: 'tokens',
  generated_at: '2026-10-20T12:00:00Z',
};

describe('useProviderSourceApps', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.fetchSourceApps).mockResolvedValue(empty);
  });

  it('asks for the apps of one provider account', async () => {
    const { result } = renderHook(() => useProviderSourceApps('minimax', 'acc-1'), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(api.fetchSourceApps).toHaveBeenCalledWith({
      provider_id: 'minimax',
      account_id: 'acc-1',
      sidecar_id: undefined,
    });
  });

  it('scopes the request to the selected range', async () => {
    const range = { since: '2026-10-01T00:00:00Z', until: '2026-10-20T00:00:00Z' };
    const { result } = renderHook(() => useProviderSourceApps('minimax', 'acc-1', range), {
      wrapper,
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(api.fetchSourceApps).toHaveBeenCalledWith({
      provider_id: 'minimax',
      account_id: 'acc-1',
      sidecar_id: undefined,
      since: range.since,
      until: range.until,
    });
  });
});
