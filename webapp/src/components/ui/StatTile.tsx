// Compact KPI tile: a label over a mono value with an optional dim hint.
// Shared by the History page and the provider-detail KPI strips.

import type { ReactNode } from 'react';
import { cn } from '@/lib/cn';
import type { QuotaStatus } from '@/lib/quota';
import { Card } from './Card';
import { Skeleton } from './Skeleton';

const STATUS_TEXT: Record<QuotaStatus, string> = {
  critical: 'text-critical',
  warning: 'text-warning',
  ok: 'text-fg',
  unlimited: 'text-fg',
  unknown: 'text-fg',
};

export function StatTile({
  label,
  value,
  hint,
  loading,
  status,
  className,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  loading?: boolean;
  // When set, tints the value by quota severity (critical/warning).
  status?: QuotaStatus;
  className?: string;
}) {
  return (
    <Card className={cn('px-4 py-3', className)}>
      <p className="text-[11px] font-medium text-fg-subtle">{label}</p>
      {loading ? (
        <Skeleton className="mt-1.5 h-6 w-20" />
      ) : (
        <div className="mt-0.5 flex items-baseline gap-2">
          <span
            className={cn(
              'font-mono text-lg font-semibold tabular',
              status ? STATUS_TEXT[status] : undefined,
            )}
          >
            {value}
          </span>
          {hint ? <span className="truncate text-[11px] text-fg-subtle">{hint}</span> : null}
        </div>
      )}
    </Card>
  );
}
