// Token composition donut: input / output / cache read / cache create /
// reasoning split for a period bucket.

import { useMemo, useState } from 'react';
import type { CumulativeModelBucket } from '@/api/types';
import { formatTokens } from '@/lib/format';
import { EChart } from './EChart';
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
  const option = useMemo(() => {
    const data = SLICES.filter(({ key }) => !excludeCache || !CACHE_KEYS.has(key))
      .map(({ key, label }) => ({
        name: label,
        value: (bucket[key] as number | undefined) ?? 0,
      }))
      .filter((d) => d.value > 0);
    const total = data
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
          data,
        },
      ],
    };
  }, [bucket, t, excludeCache, selected]);

  return (
    <EChart
      option={option}
      className={className ?? 'h-56'}
      onReady={(chart) =>
        chart.on('legendselectchanged', (e) =>
          setSelected((e as { selected: Record<string, boolean> }).selected),
        )
      }
    />
  );
}
