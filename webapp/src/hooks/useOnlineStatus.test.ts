import { act, renderHook } from '@testing-library/react';
import { useOnlineStatus } from './useOnlineStatus';

describe('useOnlineStatus', () => {
  it('reflects online/offline window events', () => {
    const { result } = renderHook(() => useOnlineStatus());
    expect(result.current).toBe(true);

    act(() => window.dispatchEvent(new Event('offline')));
    expect(result.current).toBe(false);

    act(() => window.dispatchEvent(new Event('online')));
    expect(result.current).toBe(true);
  });
});
