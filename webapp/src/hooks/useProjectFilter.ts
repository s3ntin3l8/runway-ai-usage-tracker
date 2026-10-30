import { useCallback } from 'react';
import { useSearchParams } from 'react-router';

const PARAM = 'project';

// The Sessions project filter lives in the URL (like the usage source), so a
// reload or a shared link keeps the selection.
export function useProjectFilter(): readonly [string | undefined, (value: string | undefined) => void] {
  const [searchParams, setSearchParams] = useSearchParams();
  const project = searchParams.get(PARAM) || undefined;
  const setProject = useCallback(
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
  return [project, setProject] as const;
}
