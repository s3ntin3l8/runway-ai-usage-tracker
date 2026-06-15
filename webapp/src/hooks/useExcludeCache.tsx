// Shared "exclude cache" preference. Cache read/create is ~95% of token volume
// and skews every headline; this lets the user drop it from token aggregates and
// donuts across the whole app. Persisted to localStorage so the choice survives
// tab switches and reloads — same pattern as useTheme.
import { createContext, useCallback, useContext, useState } from 'react';

const STORAGE_KEY = 'runway_exclude_cache';

interface ExcludeCacheContextValue {
  excludeCache: boolean;
  setExcludeCache: (next: boolean) => void;
}

const ExcludeCacheContext = createContext<ExcludeCacheContextValue | null>(null);

function readPref(): boolean {
  return localStorage.getItem(STORAGE_KEY) === '1';
}

export function ExcludeCacheProvider({ children }: { children: React.ReactNode }) {
  const [excludeCache, setExcludeState] = useState<boolean>(readPref);

  const setExcludeCache = useCallback((next: boolean) => {
    setExcludeState(next);
    localStorage.setItem(STORAGE_KEY, next ? '1' : '0');
  }, []);

  return (
    <ExcludeCacheContext.Provider value={{ excludeCache, setExcludeCache }}>
      {children}
    </ExcludeCacheContext.Provider>
  );
}

export function useExcludeCache(): ExcludeCacheContextValue {
  const ctx = useContext(ExcludeCacheContext);
  if (!ctx) throw new Error('useExcludeCache must be used inside <ExcludeCacheProvider>');
  return ctx;
}
