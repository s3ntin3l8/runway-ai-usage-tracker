// Session identity and assign-target encoding for the unassigned-usage card.
// Moved unchanged from PendingUsageEventsCard.

import type { PendingUsageSession } from '@/api/types';

export function sessionKey(group: PendingUsageSession) {
  const sessionIdentity = group.session_id ? `session:${group.session_id}` : `event:${group.event_ids[0]}`;
  return JSON.stringify([group.provider_id, group.sidecar_id, sessionIdentity]);
}

export function groupLabel(group: PendingUsageSession) {
  return group.session_id ?? `event ${group.event_ids[0]}`;
}

export type AssignOptionKind = 'active' | 'archived' | 'related';

export interface AssignOption {
  /** Encoded `[providerId, accountId]` — the option's <select> value. */
  value: string;
  providerId: string;
  accountId: string;
  label: string;
  kind: AssignOptionKind;
}

export interface AssignOptionGroup {
  label: string;
  options: AssignOption[];
}

export const encodeAssignTarget = (providerId: string, accountId: string) => JSON.stringify([providerId, accountId]);

export function decodeAssignTarget(value: string): { providerId: string; accountId: string } {
  const [providerId, accountId] = JSON.parse(value) as [string, string];
  return { providerId, accountId };
}
