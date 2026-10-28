// Shared access to the sidecar registry. FleetPage already fetches under this
// key; reusing it means the session views (Overview / Activity) get a cache hit
// rather than a second request.

import { useMemo } from 'react';
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

// What a usage row calls the machine it came from: the server's own collectors are
// "local", and a sidecar the registry no longer knows keeps its raw id.
export const LOCAL_SIDECAR_LABEL = 'This server';

export function sidecarLabel(names: Map<string, string>, id: string): string {
  return id === 'local' ? LOCAL_SIDECAR_LABEL : (names.get(id) ?? id);
}

// The cached registry as an id -> name lookup, for any view that shows a sidecar id.
export function useSidecarNames(): Map<string, string> {
  const { data } = useSidecars();
  return useMemo(() => buildSidecarNameMap(data?.sidecars ?? []), [data]);
}

// "device-720f1366…" -> "720f": enough to tell two same-named machines apart.
const shortSidecarId = (id: string): string => id.replace(/^device-/, '').slice(0, 4);

// Re-key a by-sidecar split from sidecar ids to the names people know them by. A paired
// device is "device-<hex>" in the data, which is meaningless in a legend. Two entries that
// end up with the same name (an old entry and its paired device, for example) keep a short
// id suffix so they stay two slices instead of silently merging into one.
export function relabelSidecarSplit<T>(
  split: Record<string, T>,
  names: Map<string, string>,
): Record<string, T> {
  const labels = Object.keys(split).map((id) => [id, sidecarLabel(names, id)] as const);
  const taken = new Map<string, number>();
  for (const [, label] of labels) taken.set(label, (taken.get(label) ?? 0) + 1);
  const out: Record<string, T> = {};
  for (const [id, label] of labels) {
    const base = (taken.get(label) ?? 0) > 1 ? `${label} (${shortSidecarId(id)})` : label;
    // Short ids can collide too (two ids that share their first four characters), and a clash
    // here would drop a bucket and understate the total. Number the final key until it is free.
    let key = base;
    for (let n = 2; key in out; n += 1) key = `${base} (${n})`;
    out[key] = split[id];
  }
  return out;
}

// Count of sidecars that have an update available. Reuses the shared
// ['fleet','sidecars'] query so the Sidebar/BottomNav badge is a cache hit,
// not an extra request. Returns 0 when the query is pending or errored.
export function useFleetUpdateCount(): number {
  const { data } = useSidecars();
  return (data?.sidecars ?? []).filter((s) => s.update_available).length;
}
