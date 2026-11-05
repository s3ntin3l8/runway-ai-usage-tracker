import type { Sidecar } from '@/api/types';

// Liveness is computed server-side (fleet_registry.to_dict's `stale` field,
// gated on stale_threshold_minutes) so there's one source of truth — a
// second, differently-tuned client-side threshold previously disagreed with
// it (30min here vs. the server's 60min), which could show a sidecar as
// "stale" in this badge while the server still offered it "update
// available". `stale` is always present in a live API response; `!s.stale`
// only defaults to online when it's genuinely absent (e.g. a stale test
// fixture).
export function isOnline(s: Sidecar): boolean {
  if (!s.last_seen) return false;
  return !s.stale;
}
