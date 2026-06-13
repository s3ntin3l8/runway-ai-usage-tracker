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
  // h-1 hairline for dense lists, h-1.5 default, h-2 hero, h-3 chunky
  size?: 'sm' | 'md' | 'lg' | 'xl';
  // Optional glide-path marker (0–100): where usage *should* be if paced evenly
  // across the window. Rendered as a pronounced full-height tick.
  glide?: number | null;
}

const HEIGHT = { sm: 'h-1', md: 'h-1.5', lg: 'h-2', xl: 'h-3' } as const;

// Linear quota bar. Values over 100% clamp visually but keep their label
// semantics (over-quota stays a full critical bar).
export function Gauge({ pct, status, className, size = 'md', glide }: GaugeProps) {
  const value = pct === null || pct === undefined || Number.isNaN(pct) ? null : pct;
  const width = value === null ? 0 : Math.min(100, Math.max(0, value));
  const showGlide = glide != null && !Number.isNaN(glide) && glide > 0 && glide < 100;
  return (
    <div
      role="meter"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={value ?? undefined}
      aria-valuetext={value === null ? 'unknown' : `${value.toFixed(0)}% used`}
      className={cn(
        'relative w-full overflow-hidden rounded-full bg-surface-3',
        HEIGHT[size],
        className,
      )}
    >
      <div
        className={cn('h-full rounded-full transition-[width] duration-300', FILL[status])}
        style={{ width: `${width}%` }}
      />
      {showGlide ? (
        <div
          aria-hidden
          title="glide-path target — where you should be if pacing evenly"
          className="absolute inset-y-0 w-[3px] -translate-x-1/2 rounded-full bg-fg ring-1 ring-canvas/60"
          style={{ left: `${glide}%` }}
        />
      ) : null}
    </div>
  );
}
