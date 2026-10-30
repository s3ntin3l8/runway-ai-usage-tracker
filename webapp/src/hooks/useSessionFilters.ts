import { useCallback } from 'react';
import { useSearchParams } from 'react-router';

// Value of the `project` URL param that selects sessions without a project
// label (the server documents the same sentinel).
export const NO_PROJECT = '__none__';

// The Sessions model / branch / subagent filters live in the URL, like the
// usage source and the project, so a reload or a shared link keeps them.
function useUrlParam(name: string): readonly [string | undefined, (value: string | undefined) => void] {
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

export const useModelFilter = () => useUrlParam('model');
export const useBranchFilter = () => useUrlParam('branch');

// `?subagents=exclude` hides subagent usage; any other value is ignored.
export function useHideSubagents(): readonly [boolean, (hide: boolean) => void] {
  const [value, setValue] = useUrlParam('subagents');
  const setHide = useCallback((hide: boolean) => setValue(hide ? 'exclude' : undefined), [setValue]);
  return [value === 'exclude', setHide] as const;
}
