import { useUrlParam } from './useUrlParam';

// The Sessions project filter lives in the URL (like the usage source), so a
// reload or a shared link keeps the selection.
export function useProjectFilter(): readonly [string | undefined, (value: string | undefined) => void] {
  return useUrlParam('project');
}
