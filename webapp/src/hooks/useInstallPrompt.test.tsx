import type { ReactNode } from 'react';
import { act, renderHook } from '@testing-library/react';
import { InstallProvider, useInstallPrompt } from './useInstallPrompt';

function wrapper({ children }: { children: ReactNode }) {
  return <InstallProvider>{children}</InstallProvider>;
}

function fakePromptEvent(outcome: 'accepted' | 'dismissed' = 'accepted') {
  return Object.assign(new Event('beforeinstallprompt'), {
    prompt: vi.fn().mockResolvedValue(undefined),
    userChoice: Promise.resolve({ outcome }),
  });
}

describe('useInstallPrompt', () => {
  it('captures beforeinstallprompt, then consumes it on promptInstall', async () => {
    const { result } = renderHook(() => useInstallPrompt(), { wrapper });
    expect(result.current.canInstall).toBe(false);

    const evt = fakePromptEvent();
    act(() => void window.dispatchEvent(evt));
    expect(result.current.canInstall).toBe(true);

    await act(async () => {
      await result.current.promptInstall();
    });
    expect(evt.prompt).toHaveBeenCalledOnce();
    // The native event can only be used once.
    expect(result.current.canInstall).toBe(false);
  });

  it('clears the prompt once the app is installed', () => {
    const { result } = renderHook(() => useInstallPrompt(), { wrapper });
    act(() => void window.dispatchEvent(fakePromptEvent('dismissed')));
    expect(result.current.canInstall).toBe(true);

    act(() => void window.dispatchEvent(new Event('appinstalled')));
    expect(result.current.canInstall).toBe(false);
  });
});
