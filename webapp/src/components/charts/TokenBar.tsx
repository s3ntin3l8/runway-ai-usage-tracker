// Compact segmented bar of a session's token composition (input / output /
// cache read / cache create / reasoning). A lightweight CSS alternative to the
// TokenDonut for tight spaces — shares its SLICES/CACHE_KEYS and palette so the
// two read identically. Colors follow the --chart-N tokens by visible position,
// matching how ECharts assigns the donut's palette after filtering.
//
// Imports slice constants from ./tokenSlices, NOT from ./TokenDonut — this
// component renders on the eager Home route, and TokenDonut.tsx statically
// imports echarts/core, which would drag the ~685KB echarts chunk into
// Home's critical path for a component that never renders a chart.

import type { TokenSliceKey } from './tokenSlices';
import { CACHE_KEYS, SLICES } from './tokenSlices';
import { formatTokens } from '@/lib/format';
import { cn } from '@/lib/cn';

export function TokenBar({
  tokens,
  excludeCache = false,
  showLegend = false,
  className,
}: {
  // Any object carrying the token-slice fields (SessionEntry, bucket, …).
  tokens: Partial<Record<TokenSliceKey, number | undefined>>;
  excludeCache?: boolean;
  showLegend?: boolean;
  className?: string;
}) {
  const segments = SLICES.filter(({ key }) => !excludeCache || !CACHE_KEYS.has(key))
    .map(({ key, label }) => ({ label, value: tokens[key] ?? 0 }))
    .filter((d) => d.value > 0)
    // Color by visible position so the bar matches the donut after filtering.
    .map((d, i) => ({ ...d, color: `var(--chart-${i + 1})` }));

  const total = segments.reduce((sum, d) => sum + d.value, 0);
  if (total === 0) return null;

  return (
    <div className={cn('flex flex-col gap-1.5', className)}>
      <div className="flex h-1.5 w-full overflow-hidden rounded-full bg-surface-2">
        {segments.map((d) => (
          <div
            key={d.label}
            style={{ width: `${(d.value / total) * 100}%`, background: d.color }}
            title={`${d.label}: ${formatTokens(d.value)}`}
          />
        ))}
      </div>
      {showLegend ? (
        <div className="flex flex-wrap gap-x-3 gap-y-1">
          {segments.map((d) => (
            <span key={d.label} className="flex items-center gap-1 text-[10px] text-fg-subtle">
              <span className="size-2 rounded-full" style={{ background: d.color }} />
              {d.label} {formatTokens(d.value)}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}
