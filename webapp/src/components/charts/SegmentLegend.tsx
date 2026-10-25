// A DOM legend for a categorical chart: one swatch and label per series.
//
// It is DOM rather than an ECharts legend so the labels are real text —
// selectable, and read by assistive technology in the order the series
// appear. The colour comes from the caller, which resolves it from the
// segment key (never from the series' position), so a segment keeps its
// colour across every chart that shows it.

import { cn } from '@/lib/cn';

export interface LegendSegment {
  /** Stable identity — the segment key, not the index. */
  key: string;
  label: string;
  /** A CSS colour: a `var(--chart-N)` reference or a resolved value. */
  color: string;
  /** An optional formatted value, rendered after the label. */
  value?: string;
}

export function SegmentLegend({
  segments,
  className,
}: {
  segments: LegendSegment[];
  className?: string;
}) {
  return (
    <div className={cn('flex flex-wrap gap-x-3 gap-y-1', className)}>
      {segments.map((s) => (
        <span key={s.key} className="flex items-center gap-1 text-[10px] text-fg-subtle">
          <span className="size-2 rounded-full" style={{ background: s.color }} />
          {s.label}
          {s.value ? ` ${s.value}` : null}
        </span>
      ))}
    </div>
  );
}
