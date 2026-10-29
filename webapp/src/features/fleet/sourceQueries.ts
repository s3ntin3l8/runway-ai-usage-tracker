// Queries behind the sources card and the badge on the dialog's Credentials tab.

import { useQuery } from '@tanstack/react-query';
import { fetchSidecarSources } from '@/api/endpoints';
import { countSources } from './sourceModel';

export function useSidecarSources(sidecarId: string, osPlatform?: string | null) {
  const macOS = osPlatform?.startsWith('Darwin') === true;
  const compatible = macOS || /^(Windows|Linux)(\/|$)/.test(osPlatform ?? '');
  const enrolled = /^device-[a-f0-9]{32}$/.test(sidecarId);
  const query = useQuery({
    queryKey: ['fleet', 'sources', sidecarId],
    queryFn: () => fetchSidecarSources(sidecarId),
    enabled: enrolled && compatible,
    refetchInterval: 30_000,
  });
  return { macOS, compatible, enrolled, query };
}

/** Enabled sources someone still has to authorize on the Mac: the badge on the dialog's Credentials tab. */
export function useSourcesNeedingAttention(sidecarId: string, osPlatform?: string | null): number {
  const { macOS, query } = useSidecarSources(sidecarId, osPlatform);
  if (!macOS) return 0;
  return countSources(query.data?.sources ?? []).attention;
}
