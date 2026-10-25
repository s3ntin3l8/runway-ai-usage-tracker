// Compact segmented bar of a session's token composition (input / output /
// cache read / cache create / reasoning). A lightweight CSS alternative to the
// TokenDonut for tight spaces — shares its SLICES/CACHE_KEYS and palette so the
// two read identically.
//
// Colors follow the SEGMENT KEY, not the visible position: the segment list
// shrinks when cache is excluded (and when a segment has no value), so a
// positional palette would move "Reasoning" to a different colour the moment
// the user toggles a filter. The slot order is shared with the donut via
// ./segmentColors.
//
// Imports slice constants from ./tokenSlices, NOT from ./TokenDonut — this
// component renders on the eager Home route, and TokenDonut.tsx statically
// imports echarts/core, which would drag the ~685KB echarts chunk into
// Home's critical path for a component that never renders a chart.

import type { TokenSliceKey } from './tokenSlices';
import { CACHE_KEYS, SLICES } from './tokenSlices';
import { segmentColorVar } from './segmentColors';
import { formatTokens } from '@/lib/format';
import { cn } from '@/lib/cn';
import { SegmentLegend } from './SegmentLegend';

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
    .map(({ key, label }) => ({ key, label, value: tokens[key] ?? 0 }))
    .filter((d) => d.value > 0)
    // Colour by segment key so a segment keeps its colour whether or not
    // cache is excluded — and matches the donut's colour for the same slice.
    .map((d) => ({ ...d, color: segmentColorVar(d.key) }));

  const total = segments.reduce((sum, d) => sum + d.value, 0);
  if (total === 0) return null;

  return (
    <div className={cn('flex flex-col gap-1.5', className)}>
      <div
        className="flex h-1.5 w-full overflow-hidden rounded-full bg-surface-2"
        // The proportions live only in the pixels and the (mouse-only)
        // title tooltips, so name the graphic for assistive tech. The label
        // sits on the bar itself, not on the wrapper, so the visible legend
        // below stays readable as text.
        role="img"
        aria-label={`Token composition: ${segments
          .map((d) => `${d.label} ${formatTokens(d.value)}`)
          .join(', ')}`}
      >
        {segments.map((d) => (
          <div
            key={d.key}
            style={{ width: `${(d.value / total) * 100}%`, background: d.color }}
            title={`${d.label}: ${formatTokens(d.value)}`}
          />
        ))}
      </div>
      {showLegend ? (
        <SegmentLegend
          segments={segments.map((d) => ({
            key: d.key,
            label: d.label,
            color: d.color,
            value: formatTokens(d.value),
          }))}
        />
      ) : null}
    </div>
  );
}
