// Per-model token split donut: one slice per model_id in a period bucket's
// by_model map, sized by that model's total tokens.

import { useMemo, useState } from 'react';
import type { CumulativeModelBucket } from '@/api/types';
import { formatTokens } from '@/lib/format';
import { EChart } from './EChart';
import { AccessibleSummary } from './accessibleSummary';
import { baseTooltip, useChartTokens } from './theme';

function modelTokens(b: CumulativeModelBucket, excludeCache: boolean): number {
  return (
    (b.tokens_input ?? 0) +
    (b.tokens_output ?? 0) +
    (excludeCache ? 0 : (b.tokens_cache_read ?? 0) + (b.tokens_cache_create ?? 0)) +
    (b.tokens_reasoning ?? 0)
  );
}

export function ModelDonut({
  byModel,
  className,
  excludeCache = false,
}: {
  byModel: Record<string, CumulativeModelBucket>;
  className?: string;
  excludeCache?: boolean;
}) {
  const t = useChartTokens();
  // Track legend deselection so the center total reflects only visible slices.
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  // Built once and shared by the chart and its text alternative.
  const slices = useMemo(
    () =>
      Object.entries(byModel)
        .map(([model, b]) => ({ name: model, value: modelTokens(b, excludeCache) }))
        .filter((d) => d.value > 0)
        .sort((a, b) => b.value - a.value),
    [byModel, excludeCache],
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
        ariaLabel="Tokens by model chart"
        onReady={(chart) =>
          chart.on('legendselectchanged', (e) =>
            setSelected((e as { selected: Record<string, boolean> }).selected),
          )
        }
      />
      {/* The legend names the models, but each model's token count exists
          only in the canvas and the hover tooltip. */}
      <AccessibleSummary
        caption="Tokens by model"
        entries={slices.map((s) => ({ label: s.name, value: formatTokens(s.value) }))}
      />
    </>
  );
}
