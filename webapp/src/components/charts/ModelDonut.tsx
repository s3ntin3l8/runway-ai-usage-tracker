// Per-model token split donut: one slice per model_id in a period bucket's
// by_model map, sized by that model's total tokens.

import { useMemo } from 'react';
import type { CumulativeModelBucket } from '@/api/types';
import { formatTokens } from '@/lib/format';
import { EChart } from './EChart';
import { baseTooltip, useChartTokens } from './theme';

function modelTokens(b: CumulativeModelBucket): number {
  return (
    (b.tokens_input ?? 0) +
    (b.tokens_output ?? 0) +
    (b.tokens_cache_read ?? 0) +
    (b.tokens_cache_create ?? 0) +
    (b.tokens_reasoning ?? 0)
  );
}

export function ModelDonut({
  byModel,
  className,
}: {
  byModel: Record<string, CumulativeModelBucket>;
  className?: string;
}) {
  const t = useChartTokens();
  const option = useMemo(() => {
    const data = Object.entries(byModel)
      .map(([model, b]) => ({ name: model, value: modelTokens(b) }))
      .filter((d) => d.value > 0)
      .sort((a, b) => b.value - a.value);
    const total = data.reduce((sum, d) => sum + d.value, 0);
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
  }, [byModel, t]);

  return <EChart option={option} className={className ?? 'h-56'} />;
}
