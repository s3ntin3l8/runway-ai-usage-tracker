import { useQuery } from '@tanstack/react-query';
import { fetchAppConfig } from '@/api/endpoints';
import type { SidecarChannel } from '@/api/types';
import type { FleetUpdateDefaults } from './settingsSync';

/** The fleet-wide auto-update flag and channel from Settings → System (shares the cached query). */
export function useFleetUpdateDefaults(): FleetUpdateDefaults {
  const { data } = useQuery({ queryKey: ['system', 'app-config'], queryFn: fetchAppConfig });
  return {
    autoUpdate: data?.sidecar_auto_update === true,
    channel: (data?.sidecar_update_channel as SidecarChannel | undefined) ?? 'stable',
  };
}
