import { useCallback } from 'react';
import { useSearchParams } from 'react-router';

// A string filter kept in the URL (like the usage source), so a reload or a
// shared link keeps the selection. Empty or absent reads as undefined.
export function useUrlParam(name: string): readonly [string | undefined, (value: string | undefined) => void] {
  const [searchParams, setSearchParams] = useSearchParams();
  const value = searchParams.get(name) || undefined;
  const set = useCallback(
    (next: string | undefined) => {
      setSearchParams(
        (prev) => {
          const params = new URLSearchParams(prev);
          if (next) params.set(name, next);
          else params.delete(name);
          return params;
        },
        { replace: true },
      );
    },
    [name, setSearchParams],
  );
  return [value, set] as const;
}
