// Device-permission and server-collection badges for one source, or a rolled-up service row.

import type { FleetSourceStatus } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { timeAgo } from '@/lib/format';
import type { MacStatus } from './sourceModel';
import { STATUS } from './sourceStatus';

const HEALTH_VARIANT = {
  healthy: 'ok',
  action_required: 'critical',
  paused: 'warning',
  withdrawn: 'neutral',
  untried: 'neutral',
} as const;

export function MacBadge({ status, device, source }: { status: MacStatus; device: string; source?: FleetSourceStatus }) {
  const s = STATUS[status];
  const withdrawing = source?.state === 'Withdrawal pending';
  const label = source?.revoked
    ? 'Revoked'
    : withdrawing
      ? 'Withdrawing'
      : s.label === 'Setup on Mac'
        ? `Setup on ${device}`
        : s.label;
  const note = withdrawing ? 'Off on this device; the server has not acknowledged the withdrawal yet' : undefined;
  return (
    <Badge variant={s.variant} title={note} className="justify-center">
      {label}
    </Badge>
  );
}

export function CollectionBadge({ source }: { source: FleetSourceStatus }) {
  if (!source.read) return <span className="text-fg-subtle">—</span>;
  return (
    <Badge variant={HEALTH_VARIANT[source.collection_health]} className="justify-center">
      {source.collection_health.replaceAll('_', ' ')}
      {source.last_collection_at ? ` · ${timeAgo(source.last_collection_at)}` : ''}
    </Badge>
  );
}

export function ServiceCollection({ sources }: { sources: FleetSourceStatus[] }) {
  const on = sources.filter((s) => s.read);
  if (!on.length) return <span className="text-fg-subtle">—</span>;
  const healthy = on.filter((s) => s.collection_health === 'healthy').length;
  if (healthy) return <Badge variant="ok" className="justify-center">{`healthy · ${healthy}/${on.length}`}</Badge>;
  if (on.some((s) => s.collection_health === 'action_required'))
    return <Badge variant="critical" className="justify-center">action required</Badge>;
  return <Badge variant="neutral" className="justify-center">untried</Badge>;
}
