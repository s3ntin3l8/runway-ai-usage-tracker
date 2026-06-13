// Token composition donut: input / output / cache read / cache create /
// reasoning split for a period bucket.

import { useMemo } from 'react';
import type { CumulativeModelBucket } from '@/api/types';
import { formatTokens } from '@/lib/format';
import { EChart } from './EChart';
import { baseTooltip, useChartTokens } from './theme';

const SLICES: { key: keyof CumulativeModelBucket; label: string }[] = [
  { key: 'tokens_input', label: 'Input' },
  { key: 'tokens_output', label: 'Output' },
  { key: 'tokens_cache_read', label: 'Cache read' },
  { key: 'tokens_cache_create', label: 'Cache create' },
  { key: 'tokens_reasoning', label: 'Reasoning' },
];

export function TokenDonut({ bucket, className }: { bucket: CumulativeModelBucket; className?: string }) {
  const t = useChartTokens();
  const option = useMemo(() => {
    const data = SLICES.map(({ key, label }) => ({
      name: label,
      value: (bucket[key] as number | undefined) ?? 0,
    })).filter((d) => d.value > 0);
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
  }, [bucket, t]);

  return <EChart option={option} className={className ?? 'h-56'} />;
}
