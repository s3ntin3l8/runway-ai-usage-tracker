import type { QuotaStatus } from '@/lib/quota';
import { cn } from '@/lib/cn';

const COLORS: Record<QuotaStatus, string> = {
  critical: 'bg-critical',
  warning: 'bg-warning',
  ok: 'bg-ok',
  unlimited: 'bg-unlimited',
  unknown: 'bg-unknown',
};

interface StatusDotProps {
  status: QuotaStatus;
  // Pulse for live-critical attention states only.
  pulse?: boolean;
  className?: string;
  label?: string;
}

export function StatusDot({ status, pulse, className, label }: StatusDotProps) {
  return (
    <span
      role="img"
      aria-label={label ?? status}
      className={cn('relative inline-flex size-2 shrink-0', className)}
    >
      {pulse ? (
        <span
          className={cn('absolute inline-flex size-full animate-ping rounded-full opacity-60', COLORS[status])}
        />
      ) : null}
      <span className={cn('relative inline-flex size-2 rounded-full', COLORS[status])} />
    </span>
  );
}
