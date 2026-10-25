// Cost distribution donut: one slice per key (model_id or sidecar) in a
// period bucket's by_model / by_sidecar map, sized by that entry's cost in USD.

import { useMemo, useState } from 'react';
import type { CumulativeModelBucket } from '@/api/types';
import { formatCost } from '@/lib/format';
import { EChart } from './EChart';
import { AccessibleSummary } from './accessibleSummary';
import { baseTooltip, capChartSlices, useChartTokens } from './theme';

// Cost for one bucket. Excluding cache uses the token-priced non-cache
// components; this remains meaningful when the displayed total is a source
// reported amount with a separate best-effort token breakdown.
export function modelCost(b: CumulativeModelBucket, excludeCache: boolean): number {
  return excludeCache
    ? (b.cost_input ?? 0) + (b.cost_output ?? 0)
    : b.cost_usd ?? 0;
}

export function CostDonut({
  data,
  className,
  excludeCache = false,
}: {
  data: Record<string, CumulativeModelBucket>;
  className?: string;
  excludeCache?: boolean;
}) {
  const t = useChartTokens();
  // Track legend deselection so the center total reflects only visible slices.
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  // Built once and shared by the chart and its text alternative.
  const slices = useMemo(
    () =>
      Object.entries(data)
        .map(([name, b]) => ({ name, value: modelCost(b, excludeCache) }))
        .filter((d) => d.value > 0)
        .sort((a, b) => b.value - a.value),
    [data, excludeCache],
  );
  // The palette has six colours: the entries past the sixth share one
  // "Other (N)" slice instead of repeating a colour. The slice values still
  // add up to every entry's cost, so the total in the middle stays the true
  // one.
  const capped = useMemo(() => capChartSlices(slices), [slices]);
  const option = useMemo(() => {
    const total = capped.slices
      .filter((d) => selected[d.name] !== false)
      .reduce((sum, d) => sum + d.value, 0);
    return {
      // The last colour is the grey the "Other" slice takes; the six
      // categorical colours ahead of it belong to the drawn entries.
      color: [...t.series, t.fgMuted],
      tooltip: {
        ...baseTooltip(t),
        formatter: (p: { name: string; value: number; percent: number }) =>
          `${p.name}: ${formatCost(p.value)} (${p.percent}%)`,
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
            formatter: () => formatCost(total),
            color: t.fg,
            fontSize: 18,
            fontWeight: 600,
            fontFamily: t.monoFamily,
          },
          emphasis: { label: { show: true } },
          data: capped.slices,
        },
      ],
    };
  }, [capped, t, selected]);

  return (
    <>
      <EChart
        option={option}
        className={className ?? 'h-56'}
        ariaLabel="Cost by model chart"
        onReady={(chart) =>
          chart.on('legendselectchanged', (e) =>
            setSelected((e as { selected: Record<string, boolean> }).selected),
          )
        }
      />
      {/* The legend names the entries, but each cost exists only in the
          canvas and the hover tooltip. The summary lists the slices the
          chart draws, so it cannot claim more entries than the chart shows. */}
      <AccessibleSummary
        caption="Cost by model"
        entries={capped.slices.map((s) => ({ label: s.name, value: formatCost(s.value) }))}
      />
    </>
  );
}
