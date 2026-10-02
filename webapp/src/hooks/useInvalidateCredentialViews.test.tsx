import { renderHook } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { credentialInventoryKey, useInvalidateCredentialViews } from './useInvalidateCredentialViews';

describe('useInvalidateCredentialViews', () => {
  it('invalidates every view that reads credential or identity state', () => {
    const client = new QueryClient();
    const spy = vi.spyOn(client, 'invalidateQueries');
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useInvalidateCredentialViews(), { wrapper });

    result.current();

    // Includes the keys the old dialog missed or misspelled ('provider_configs').
    expect(spy.mock.calls.map(([arg]) => arg?.queryKey)).toEqual([
      credentialInventoryKey,
      ['system', 'provider-configs'],
      ['fleet'],
      ['usage'],
    ]);
  });
});
