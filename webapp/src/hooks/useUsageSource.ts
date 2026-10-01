import { useCallback } from 'react';
import { useSearchParams } from 'react-router';

const PARAM = 'sidecar';

export function useUsageSource(): readonly [string | undefined, (value: string | undefined) => void] {
  const [searchParams, setSearchParams] = useSearchParams();
  const sidecarId = searchParams.get(PARAM) || undefined;
  const setSidecarId = useCallback(
    (value: string | undefined) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (value) next.set(PARAM, value);
          else next.delete(PARAM);
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );
  return [sidecarId, setSidecarId] as const;
}
