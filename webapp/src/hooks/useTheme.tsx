import { createContext, useCallback, useContext, useEffect, useState } from 'react';

export type ThemePref = 'dark' | 'light' | 'system';

const STORAGE_KEY = 'runway_theme';

interface ThemeContextValue {
  pref: ThemePref;
  setPref: (pref: ThemePref) => void;
  resolved: 'dark' | 'light';
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

function systemTheme(): 'dark' | 'light' {
  return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}

function readPref(): ThemePref {
  const stored = localStorage.getItem(STORAGE_KEY);
  return stored === 'dark' || stored === 'light' || stored === 'system' ? stored : 'system';
}

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [pref, setPrefState] = useState<ThemePref>(readPref);
  const resolved = pref === 'system' ? systemTheme() : pref;

  useEffect(() => {
    document.documentElement.dataset.theme = resolved;
  }, [resolved]);

  // Track OS theme changes while in "system" mode.
  useEffect(() => {
    if (pref !== 'system') return;
    const mql = window.matchMedia('(prefers-color-scheme: light)');
    const apply = () => {
      document.documentElement.dataset.theme = systemTheme();
    };
    mql.addEventListener('change', apply);
    return () => mql.removeEventListener('change', apply);
  }, [pref]);

  const setPref = useCallback((next: ThemePref) => {
    setPrefState(next);
    localStorage.setItem(STORAGE_KEY, next);
  }, []);

  return <ThemeContext.Provider value={{ pref, setPref, resolved }}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error('useTheme must be used inside <ThemeProvider>');
  return ctx;
}
