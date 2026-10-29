// Pure model behind the Keychain/credential sources card in a sidecar's settings.
// The tile counts and the visible rows both come from `matches`, so a tile can never
// claim a number of sources the list does not show.

import type { FleetSourceStatus } from '@/api/types';

/** What the sidecar says about its own permission, folded to the five states the card shows. */
export type MacStatus = 'ok' | 'need' | 'busy' | 'err' | 'off';
export type SourceFilter = 'on' | 'collecting' | 'attention' | 'off';

const BROWSERS: Record<string, string> = { chrome: 'Chrome', edge: 'Edge', chromium: 'Chromium' };
// Worst first when several browsers roll up into one service row.
const RANK: Record<MacStatus, number> = { off: 0, ok: 1, busy: 2, need: 3, err: 4 };

export function macStatus(s: FleetSourceStatus): MacStatus {
  if (!s.read) return 'off';
  if (s.revoked || s.state === 'Action required') return 'err';
  if (s.state === 'Connecting') return 'busy';
  if (s.state === 'Setup required') return 'need';
  return 'ok'; // Ready, Limited: usable, renewal may still be limited
}

/** The browser a cookie source reads from, or null for an app or CLI source. */
export function sourceBrowser(s: FleetSourceStatus): string | null {
  return BROWSERS[s.source_id.slice(s.source_id.lastIndexOf('.') + 1)] ?? null;
}

/** "Claude Pro · Chrome cookies" -> "Claude Pro". */
export function serviceLabel(s: FleetSourceStatus): string {
  return s.label.replace(/ · (Chrome|Edge|Chromium) cookies$/, '');
}

export function matches(filter: SourceFilter, s: FleetSourceStatus): boolean {
  const status = macStatus(s);
  switch (filter) {
    case 'on':
      return s.read;
    case 'off':
      return !s.read;
    case 'collecting':
      return s.read && s.collection_health === 'healthy';
    case 'attention':
      return s.read && (status === 'need' || status === 'err' || s.collection_health === 'action_required');
  }
}

/**
 * Sources that are on but cannot be read until someone authorizes them on the Mac. A source the
 * server revoked is not one: authorizing locally does not undo a revocation.
 */
export function needsAuthorization(s: FleetSourceStatus): boolean {
  const status = macStatus(s);
  return !s.revoked && (status === 'need' || status === 'err');
}

export interface SourceCounts {
  total: number;
  on: number;
  collecting: number;
  attention: number;
  off: number;
}

export function countSources(sources: FleetSourceStatus[]): SourceCounts {
  return {
    total: sources.length,
    on: sources.filter((s) => matches('on', s)).length,
    collecting: sources.filter((s) => matches('collecting', s)).length,
    attention: sources.filter((s) => matches('attention', s)).length,
    off: sources.filter((s) => matches('off', s)).length,
  };
}

/** Unfiltered: what is on, plus what is off once the fold is opened. Filtered: only the matches. */
export function isShown(s: FleetSourceStatus, filter: SourceFilter | null, showOff: boolean): boolean {
  if (filter) return matches(filter, s);
  return s.read || showOff;
}

export interface ServiceGroup {
  key: string;
  label: string;
  sources: FleetSourceStatus[];
}

/** Apps and CLIs stay one row each; cookie sources collapse to one row per service. */
export function groupSources(sources: FleetSourceStatus[]): { apps: FleetSourceStatus[]; services: ServiceGroup[] } {
  const apps: FleetSourceStatus[] = [];
  const services = new Map<string, ServiceGroup>();
  for (const s of sources) {
    if (sourceBrowser(s) === null) {
      apps.push(s);
      continue;
    }
    const key = s.source_id.slice(0, s.source_id.lastIndexOf('.'));
    const group = services.get(key) ?? { key, label: serviceLabel(s), sources: [] };
    group.sources.push(s);
    services.set(key, group);
  }
  return { apps, services: [...services.values()] };
}

/** The worst status among the sources a service row currently shows. */
export function rollup(sources: FleetSourceStatus[]): MacStatus {
  return sources.map(macStatus).reduce<MacStatus>((worst, s) => (RANK[s] > RANK[worst] ? s : worst), 'off');
}
