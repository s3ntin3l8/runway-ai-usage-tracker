import { useCallback } from 'react';
import { useUrlParam } from './useUrlParam';

// Value of the `project` URL param that selects sessions without a project
// label (the server documents the same sentinel).
export const NO_PROJECT = '__none__';

// The Sessions model / branch / subagent filters live in the URL too.
export const useModelFilter = () => useUrlParam('model');
export const useBranchFilter = () => useUrlParam('branch');

// `?subagents=exclude` hides subagent usage; any other value is ignored.
export function useHideSubagents(): readonly [boolean, (hide: boolean) => void] {
  const [value, setValue] = useUrlParam('subagents');
  const setHide = useCallback((hide: boolean) => setValue(hide ? 'exclude' : undefined), [setValue]);
  return [value === 'exclude', setHide] as const;
}
