// Drill-down for one quota window: fill-up curve + token breakdown + per-model table.

import { useMemo } from 'react';
import type { HistoryWindowRow, WindowDetailModelEntry } from '@/api/types';
import { EChart } from '@/components/charts/EChart';
import { baseAxisStyle, baseTooltip, useChartTokens } from '@/components/charts/theme';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';
import { Skeleton } from '@/components/ui/Skeleton';
import { Table, TBody, TD, TH, THead, TR } from '@/components/ui/Table';
import { formatCost, formatTokens } from '@/lib/format';
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

  const byModel = detail.data!.by_model ?? [];

  return (
    <div className="flex flex-col gap-4">
      {option && detail.data!.fill_series.length > 0 ? (
        <EChart option={option} className="h-64" />
      ) : (
        <p className="py-6 text-center text-xs text-fg-subtle">No fill data recorded.</p>
      )}

      {row.tokens_total != null && row.tokens_total > 0 ? (
        <TokenBreakdown row={row} />
      ) : null}

      {byModel.length > 0 ? <ModelTable models={byModel} /> : null}
    </div>
  );
}

function TokenBreakdown({ row }: { row: HistoryWindowRow }) {
  return (
    <div className="flex flex-wrap gap-4 rounded-lg border border-edge p-3">
      <span className="text-xs font-medium text-fg">Token breakdown</span>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
        <span className="text-fg-muted">
          Input: <span className="font-mono tabular text-fg">{formatTokens(row.tokens_input)}</span>
        </span>
        <span className="text-fg-muted">
          Output:{' '}
          <span className="font-mono tabular text-fg">{formatTokens(row.tokens_output)}</span>
        </span>
        <span className="text-fg-muted">
          Reasoning:{' '}
          <span className="font-mono tabular text-fg">{formatTokens(row.tokens_reasoning)}</span>
        </span>
        <span className="text-fg-muted">
          Cache read:{' '}
          <span className="font-mono tabular text-fg">{formatTokens(row.tokens_cache_read)}</span>
        </span>
        <span className="text-fg-muted">
          Cache create:{' '}
          <span className="font-mono tabular text-fg">{formatTokens(row.tokens_cache_create)}</span>
        </span>
        <span className="font-medium text-fg">
          Total: <span className="font-mono tabular">{formatTokens(row.tokens_total)}</span>
        </span>
      </div>
    </div>
  );
}

function ModelTable({ models }: { models: WindowDetailModelEntry[] }) {
  return (
    <div>
      <p className="mb-2 text-xs font-medium text-fg">Per-model breakdown</p>
      <Table>
        <THead>
          <TR>
            <TH>Model</TH>
            <TH className="text-right">Tokens</TH>
            <TH className="hidden text-right sm:table-cell">Input</TH>
            <TH className="hidden text-right sm:table-cell">Output</TH>
            <TH className="hidden text-right md:table-cell">Cache</TH>
            <TH className="text-right">Cost</TH>
          </TR>
        </THead>
        <TBody>
          {models.map((m) => (
            <TR key={m.model_id}>
              <TD className="max-w-[140px] truncate text-xs" title={m.model_id}>
                {m.model_id}
              </TD>
              <TD className="text-right font-mono text-xs tabular">{formatTokens(m.tokens_total)}</TD>
              <TD className="hidden text-right font-mono text-xs tabular sm:table-cell">
                {formatTokens(m.tokens_input)}
              </TD>
              <TD className="hidden text-right font-mono text-xs tabular sm:table-cell">
                {formatTokens(m.tokens_output)}
              </TD>
              <TD className="hidden text-right font-mono text-xs tabular md:table-cell">
                {formatTokens(m.tokens_cache_read + m.tokens_cache_create)}
              </TD>
              <TD className="text-right font-mono text-xs tabular">{formatCost(m.cost_usd)}</TD>
            </TR>
          ))}
        </TBody>
      </Table>
    </div>
  );
}
