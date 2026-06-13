// Audit log: append-only record of admin mutations.

import { useQuery } from '@tanstack/react-query';
import { FileClock } from 'lucide-react';
import { fetchAuditLog } from '@/api/endpoints';
import { EmptyState } from '@/components/ui/EmptyState';
import { Skeleton } from '@/components/ui/Skeleton';
import { Table, TBody, TD, TH, THead, TR } from '@/components/ui/Table';
import { Card } from '@/components/ui/Card';
import { formatLocalDateTime } from '@/lib/tz';

export function AuditSection() {
  const audit = useQuery({
    queryKey: ['system', 'audit-log'],
    queryFn: () => fetchAuditLog(200),
    retry: false,
  });

  if (audit.isPending) return <Skeleton className="h-64" />;
  if (audit.isError) {
    return (
      <EmptyState icon={FileClock} title="Audit log unavailable" description={audit.error.message} />
    );
  }
  if ((audit.data?.entries.length ?? 0) === 0) {
    return (
      <EmptyState
        icon={FileClock}
        title="No admin mutations recorded"
        description="Sidecar controls, provider config changes and maintenance actions land here."
      />
    );
  }

  return (
    <Card>
      <Table>
        <THead>
          <TR>
            <TH>Time</TH>
            <TH>Actor</TH>
            <TH>Action</TH>
            <TH className="hidden md:table-cell">Target</TH>
            <TH className="hidden lg:table-cell">Source IP</TH>
          </TR>
        </THead>
        <TBody>
          {audit.data!.entries.map((e) => (
            <TR key={e.id}>
              <TD className="font-mono text-xs whitespace-nowrap tabular">
                {formatLocalDateTime(e.ts, {
                  month: 'short',
                  day: 'numeric',
                  hour: '2-digit',
                  minute: '2-digit',
                })}
              </TD>
              <TD className="max-w-32 truncate text-xs">{e.actor}</TD>
              <TD className="font-mono text-xs">{e.action}</TD>
              <TD className="hidden max-w-40 truncate text-xs text-fg-muted md:table-cell">
                {e.target_id ?? '—'}
              </TD>
              <TD className="hidden font-mono text-xs text-fg-subtle lg:table-cell">
                {e.source_ip ?? '—'}
              </TD>
            </TR>
          ))}
        </TBody>
      </Table>
    </Card>
  );
}
