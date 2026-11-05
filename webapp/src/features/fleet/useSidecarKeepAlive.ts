import type { Sidecar } from '@/api/types';
import { keepAliveState, useKeepAliveFleetDefault } from '@/lib/keepAlive';

/** What the sidecar runs now vs. what Runway asked for; shared by the card and the dialog. */
export function useSidecarKeepAlive(sidecar: Sidecar, online: boolean) {
  const fleetDefault = useKeepAliveFleetDefault();
  return keepAliveState({
    reported: sidecar.keep_alive ?? null,
    desired: sidecar.keep_alive_desired ?? null,
    offline: !online,
    fleetDefault,
  });
}
