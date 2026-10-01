import { useQuery } from '@tanstack/react-query';
import { fetchUsageSources } from '@/api/endpoints';
import { useSidecars, sidecarDisplayName } from '@/features/fleet/queries';
import { useUsageSource } from '@/hooks/useUsageSource';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './Select';

const ALL = '__all__';

export function SidecarFilter() {
  const [sidecarId, setSidecarId] = useUsageSource();
  const sources = useQuery({
    queryKey: ['usage', 'sources'],
    queryFn: fetchUsageSources,
    staleTime: 60_000,
  });
  const sidecars = useSidecars();
  const names = new Map(
    (sidecars.data?.sidecars ?? []).map((sidecar) => [sidecar.sidecar_id, sidecarDisplayName(sidecar)]),
  );
  const ids = new Set(sources.data?.sidecar_ids ?? []);
  if (sidecarId) ids.add(sidecarId);

  return (
    <Select value={sidecarId ?? ALL} onValueChange={(value) => setSidecarId(value === ALL ? undefined : value)}>
      <SelectTrigger className="w-32 sm:w-40" aria-label="Usage source">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={ALL}>All sources</SelectItem>
        {[...ids].sort().map((id) => (
          <SelectItem key={id} value={id}>
            {id === 'local' ? 'This server' : names.get(id) ?? id}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
