// Token composition donut: input / output / cache read / cache create /
// reasoning split for a period bucket.

import { useMemo, useState } from 'react';
import type { CumulativeModelBucket } from '@/api/types';
import { formatTokens } from '@/lib/format';
import { EChart } from './EChart';
import { AccessibleSummary } from './accessibleSummary';
import { segmentPaletteIndex } from './segmentColors';
import { baseTooltip, useChartTokens } from './theme';
import { CACHE_KEYS, SLICES } from './tokenSlices';

// Slice definitions live in ./tokenSlices (chart-free — see that file's
// header for why); re-exported here so existing `from './TokenDonut'`
// imports of the type keep working.
export type { TokenSliceKey } from './tokenSlices';

export function TokenDonut({
  bucket,
  className,
  excludeCache = false,
}: {
  bucket: CumulativeModelBucket;
  className?: string;
  excludeCache?: boolean;
}) {
  const t = useChartTokens();
  // Track legend deselection so the center total reflects only visible slices.
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  // The slices are built once and shared by the chart and its text
  // alternative, so the two can never disagree about the data.
  const slices = useMemo(
    () =>
      SLICES.filter(({ key }) => !excludeCache || !CACHE_KEYS.has(key))
        .map(({ key, label }) => ({
          name: label,
          value: (bucket[key] as number | undefined) ?? 0,
          // Colour by segment key, not by position: the slice list shrinks
          // when cache is excluded and when a slice has no value, so a
          // positional palette would renumber the colours under the user.
          itemStyle: { color: t.series[segmentPaletteIndex(key)] },
        }))
        .filter((d) => d.value > 0),
    [bucket, t, excludeCache],
  );
  const option = useMemo(() => {
    const total = slices
      .filter((d) => selected[d.name] !== false)
      .reduce((sum, d) => sum + d.value, 0);
    return {
      color: t.series,
      tooltip: {
        ...baseTooltip(t),
        formatter: (p: { name: string; value: number; percent: number }) =>
          `${p.name}: ${formatTokens(p.value)} (${p.percent}%)`,
      },
      legend: {
        bottom: 0,
        icon: 'circle',
        itemWidth: 8,
        itemHeight: 8,
        textStyle: { color: t.fgMuted, fontSize: 11, fontFamily: t.fontFamily },
        selected,
      },
      series: [
        {
          type: 'pie',
          radius: ['58%', '78%'],
          center: ['50%', '42%'],
          avoidLabelOverlap: true,
          itemStyle: { borderColor: t.surface, borderWidth: 2 },
          label: {
            show: true,
            position: 'center',
            formatter: () => formatTokens(total),
            color: t.fg,
            fontSize: 18,
            fontWeight: 600,
            fontFamily: t.monoFamily,
          },
          emphasis: { label: { show: true } },
          data: slices,
        },
      ],
    };
  }, [slices, t, selected]);

  return (
    <>
      <EChart
        option={option}
        className={className ?? 'h-56'}
        ariaLabel="Token composition chart"
        onReady={(chart) =>
          chart.on('legendselectchanged', (e) =>
            setSelected((e as { selected: Record<string, boolean> }).selected),
          )
        }
      />
      {/* The slice values live only in the canvas and the hover tooltip, so
          they need a text alternative. The legend toggles a slice off the
          chart but not out of the data, so every slice stays listed here. */}
      <AccessibleSummary
        caption="Token composition"
        entries={slices.map((s) => ({ label: s.name, value: formatTokens(s.value) }))}
      />
    </>
  );
}
