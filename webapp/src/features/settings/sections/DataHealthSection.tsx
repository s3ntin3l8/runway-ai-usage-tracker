// Settings → Data health: read-only checks over the database that surface
// correctable data-quality problems, plus an in-app fix for each. See
// app/services/data_health/ for the backend checks this drives.

import { RefreshCw, ShieldCheck } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/Button';
import { EmptyState } from '@/components/ui/EmptyState';
import { Skeleton } from '@/components/ui/Skeleton';
import { CheckRow } from './dataHealth/CheckRow';
import { useDataHealthReport, useRescanDataHealth } from './dataHealth/queries';

const SEVERITY_ORDER = { error: 0, warn: 1, info: 2 } as const;

export function DataHealthSection() {
  const report = useDataHealthReport();
  const rescan = useRescanDataHealth();

  const checks = [...(report.data?.checks ?? [])].sort(
    (a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity],
  );
  const allClean = checks.length > 0 && checks.every((c) => c.total_count === 0);

  return (
    <div className="flex max-w-2xl flex-col gap-3">
      <div className="flex items-center justify-between gap-3">
        <p className="text-[12px] text-fg-subtle">
          {report.data?.scanning
            ? 'Scanning…'
            : report.data
              ? 'Last scan complete'
              : 'No scan yet'}
        </p>
        <Button
          size="sm"
          variant="secondary"
          onClick={() =>
            rescan.mutate(undefined, {
              onError: (err) => toast.error(err.message),
            })
          }
          loading={rescan.isPending}
          disabled={report.data?.scanning}
        >
          <RefreshCw className="size-3.5" aria-hidden /> Re-scan
        </Button>
      </div>

      {report.isPending ? (
        <Skeleton className="h-24" />
      ) : report.isError ? (
        <EmptyState
          icon={ShieldCheck}
          title="Could not load Data health"
          description={report.error.message}
          action={
            <Button size="sm" onClick={() => report.refetch()}>
              Retry
            </Button>
          }
        />
      ) : allClean ? (
        <EmptyState
          icon={ShieldCheck}
          title="All checks clean"
          description="No data-quality issues found in the last scan."
        />
      ) : (
        checks.map((check) => <CheckRow key={check.check_id} check={check} />)
      )}
    </div>
  );
}
