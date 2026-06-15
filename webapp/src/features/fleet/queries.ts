// Shared access to the sidecar registry. FleetPage already fetches under this
// key; reusing it means the session views (Overview / Activity) get a cache hit
// rather than a second request.

import { useQuery } from '@tanstack/react-query';
import { fetchSidecars } from '@/api/endpoints';
import type { Sidecar } from '@/api/types';

export function useSidecars() {
  return useQuery({
    queryKey: ['fleet', 'sidecars'],
    queryFn: fetchSidecars,
    refetchInterval: 60_000,
  });
}

// User-facing label for a sidecar: prefer the custom name, then the hostname,
// then the raw id. Mirrors the inline rule used in FleetPage.
export function sidecarDisplayName(s: Sidecar): string {
  return s.custom_name || s.hostname || s.sidecar_id;
}

// sidecar_id → display name, for O(1) lookup when rendering session rows.
export function buildSidecarNameMap(sidecars: Sidecar[]): Map<string, string> {
  return new Map(sidecars.map((s) => [s.sidecar_id, sidecarDisplayName(s)]));
}
