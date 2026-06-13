import { useSyncExternalStore } from 'react';

export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (notify) => {
      const mql = window.matchMedia(query);
      mql.addEventListener('change', notify);
      return () => mql.removeEventListener('change', notify);
    },
    () => window.matchMedia(query).matches,
  );
}

// Breakpoint where the sidebar replaces the bottom nav (Tailwind lg).
export const useIsDesktop = () => useMediaQuery('(min-width: 1024px)');
