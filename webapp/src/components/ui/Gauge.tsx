import type { QuotaStatus } from '@/lib/quota';
import { cn } from '@/lib/cn';

const FILL: Record<QuotaStatus, string> = {
  critical: 'bg-critical',
  warning: 'bg-warning',
  ok: 'bg-ok',
  unlimited: 'bg-unlimited',
  unknown: 'bg-unknown',
};

interface GaugeProps {
  pct: number | null | undefined;
  status: QuotaStatus;
  className?: string;
  // h-1 hairline for dense lists, h-1.5 default, h-2 hero
  size?: 'sm' | 'md' | 'lg';
}

const HEIGHT = { sm: 'h-1', md: 'h-1.5', lg: 'h-2' } as const;

// Linear quota bar. Values over 100% clamp visually but keep their label
// semantics (over-quota stays a full critical bar).
export function Gauge({ pct, status, className, size = 'md' }: GaugeProps) {
  const value = pct === null || pct === undefined || Number.isNaN(pct) ? null : pct;
  const width = value === null ? 0 : Math.min(100, Math.max(0, value));
  return (
    <div
      role="meter"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={value ?? undefined}
      aria-valuetext={value === null ? 'unknown' : `${value.toFixed(0)}% used`}
      className={cn('w-full overflow-hidden rounded-full bg-surface-3', HEIGHT[size], className)}
    >
      <div
        className={cn('h-full rounded-full transition-[width] duration-300', FILL[status])}
        style={{ width: `${width}%` }}
      />
    </div>
  );
}
