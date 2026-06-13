// Drill-down for one quota window: fill-up curve + per-model fill series.

import { useMemo } from 'react';
import type { HistoryWindowRow } from '@/api/types';
import { EChart } from '@/components/charts/EChart';
import { baseAxisStyle, baseTooltip, useChartTokens } from '@/components/charts/theme';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';
import { Skeleton } from '@/components/ui/Skeleton';
import { formatLocalDate } from '@/lib/tz';
import { useWindowDetail } from './queries';

export function WindowDetailSheet({
  row,
  onClose,
}: {
  row: HistoryWindowRow | null;
  onClose: () => void;
}) {
  return (
    <ResponsiveDialog
      open={row !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title={row ? `${row.service_name ?? row.provider_id} · ${row.window_type}` : ''}
      description={
        row?.window_start
          ? `${formatLocalDate(row.window_start)} – ${formatLocalDate(row.window_end)}`
          : undefined
      }
      width="max-w-2xl"
    >
      {row ? <DetailBody row={row} /> : null}
    </ResponsiveDialog>
  );
}

function DetailBody({ row }: { row: HistoryWindowRow }) {
  const detail = useWindowDetail(row);
  const t = useChartTokens();

  const option = useMemo(() => {
    if (!detail.data) return null;
    const byModel = detail.data.fill_by_model.filter((m) => m.model_id !== '');
    const series = [
      {
        name: 'Total',
        type: 'line' as const,
        showSymbol: false,
        data: detail.data.fill_series.map((p) => [Date.parse(p.ts), p.pct_used]),
        lineStyle: { width: 2, color: t.accent },
        itemStyle: { color: t.accent },
        areaStyle: { color: t.accent, opacity: 0.08 },
      },
      ...byModel.map((m, i) => ({
        name: m.model_id,
        type: 'line' as const,
        showSymbol: false,
        data: m.series.map((p) => [Date.parse(p.ts), p.pct_used]),
        lineStyle: { width: 1.25, type: 'dashed' as const, color: t.series[i % t.series.length] },
        itemStyle: { color: t.series[i % t.series.length] },
      })),
    ];
    return {
      tooltip: {
        trigger: 'axis' as const,
        ...baseTooltip(t),
        valueFormatter: (v: number | null) => (v == null ? '—' : `${Number(v).toFixed(0)}%`),
      },
      legend: {
        bottom: 0,
        type: 'scroll' as const,
        icon: 'circle',
        itemWidth: 8,
        itemHeight: 8,
        textStyle: { color: t.fgMuted, fontSize: 11, fontFamily: t.fontFamily },
      },
      grid: { left: 36, right: 12, top: 12, bottom: 40 },
      xAxis: { type: 'time' as const, ...baseAxisStyle(t), splitLine: { show: false } },
      yAxis: {
        type: 'value' as const,
        max: 100,
        ...baseAxisStyle(t),
        axisLabel: { color: t.axis, fontSize: 10, fontFamily: t.fontFamily, formatter: '{value}%' },
      },
      series,
    };
  }, [detail.data, t]);

  if (!row.window_start || !row.window_end) {
    return <p className="py-6 text-center text-xs text-fg-subtle">No boundaries for this window.</p>;
  }
  if (detail.isPending) return <Skeleton className="h-64 w-full" />;
  if (detail.isError) {
    return (
      <p className="py-6 text-center text-xs text-critical">
        Could not load window detail: {detail.error.message}
      </p>
    );
  }
  if (!option || detail.data!.fill_series.length === 0) {
    return <p className="py-6 text-center text-xs text-fg-subtle">No fill data recorded.</p>;
  }
  return <EChart option={option} className="h-64" />;
}
