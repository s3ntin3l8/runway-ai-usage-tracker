import type { ReactNode } from 'react';
import { act, render, renderHook, screen } from '@testing-library/react';
import { ThemeProvider, useTheme } from './useTheme';

function wrapper({ children }: { children: ReactNode }) {
  return <ThemeProvider>{children}</ThemeProvider>;
}

// matchMedia that reports a fixed prefers-color-scheme result.
function setSystemLight(light: boolean) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: query.includes('light') ? light : false,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia;
}

describe('useTheme', () => {
  beforeEach(() => {
    localStorage.clear();
    delete document.documentElement.dataset.theme;
  });
  afterEach(() => vi.unstubAllGlobals());

  it('throws when used outside a ThemeProvider', () => {
    // Silence the expected React error boundary log.
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    expect(() => renderHook(() => useTheme())).toThrow(/inside <ThemeProvider>/);
    spy.mockRestore();
  });

  it('defaults to system and resolves to dark when the OS is dark', () => {
    setSystemLight(false);
    const { result } = renderHook(() => useTheme(), { wrapper });
    expect(result.current.pref).toBe('system');
    expect(result.current.resolved).toBe('dark');
  });

  it('resolves to light when the OS prefers light', () => {
    setSystemLight(true);
    const { result } = renderHook(() => useTheme(), { wrapper });
    expect(result.current.resolved).toBe('light');
  });

  it('reads the stored preference and applies it to the document', () => {
    setSystemLight(false);
    localStorage.setItem('runway_theme', 'light');
    const { result } = renderHook(() => useTheme(), { wrapper });
    expect(result.current.pref).toBe('light');
    expect(result.current.resolved).toBe('light');
    expect(document.documentElement.dataset.theme).toBe('light');
  });

  it('setPref updates state and persists to localStorage', () => {
    setSystemLight(false);
    const { result } = renderHook(() => useTheme(), { wrapper });
    act(() => result.current.setPref('light'));
    expect(result.current.pref).toBe('light');
    expect(localStorage.getItem('runway_theme')).toBe('light');
    expect(document.documentElement.dataset.theme).toBe('light');
  });

  it('renders its children', () => {
    setSystemLight(false);
    render(
      <ThemeProvider>
        <span>child</span>
      </ThemeProvider>,
    );
    expect(screen.getByText('child')).toBeInTheDocument();
  });
});
