// Generic horizontal ranking bar — one bar per row, largest at the top.
// Supports simple single-value bars AND stacked bars with breakdown data.
// Shared by the Top Models, Top Projects, Top Sessions and Top Hosts cards.

import { useMemo } from 'react';
import { EChart } from './EChart';
import { AccessibleSummary } from './accessibleSummary';
import { baseAxisStyle, baseTooltip, useChartTokens } from './theme';

export interface RankSegment {
  key: string;
  label: string;
  color: string;
  value: number;
}

export interface RankRow {
  label: string;
  /**
   * Bar length and sort key. In stacked mode this is the sum of the segments
   * the bar draws: the bar must agree with the segments under it.
   */
  value: number;
  /**
   * The backend's authoritative total, when it is NOT the sum of the drawn
   * segments (a provider that reports its own cost). RankBar discloses it
   * next to the drawn total instead of drawing a longer bar that contradicts
   * the segments under it.
   */
  reportedValue?: number;
  sub?: string; // optional dim line in the tooltip
  segments?: RankSegment[]; // when present, renders stacked bars
}

/**
 * The pixel budget for a y-axis label. ECharts measures the text and cuts it
 * to fit (`overflow: 'truncate'`), so a long model or project name keeps as
 * much of itself as the chart has room for — instead of being sliced at a
 * fixed character count, which cut names mid-word.
 */
const Y_AXIS_LABEL_WIDTH = 120;

/**
 * Below this relative difference the drawn total and the reported total are
 * the same number as far as the reader is concerned: float noise
 * (0.1 + 0.2 = 0.30000000000000004) is not a disagreement.
 */
const REPORTED_EPSILON = 1e-9;

/**
 * The backend total to disclose for a row, or undefined when the two numbers
 * agree.
 *
 * A difference counts as a disagreement only when it is more than a rounding
 * error AND the two numbers render differently: a difference the formatter
 * rounds away (a fraction of a cent) is invisible to the reader, so printing
 * both numbers would only look like a bug.
 */
function disclosedReported(row: RankRow, format: (v: number) => string): number | undefined {
  const reported = row.reportedValue;
  if (reported === undefined || !Number.isFinite(reported)) return undefined;
  const difference = Math.abs(reported - row.value);
  const rounding = Math.max(REPORTED_EPSILON, Math.abs(row.value) * REPORTED_EPSILON);
  if (difference <= rounding) return undefined;
  if (format(reported) === format(row.value)) return undefined;
  return reported;
}

/** The note under a row: its subtitle plus the disclosed backend total. */
function rowNote(row: RankRow, format: (v: number) => string): string | undefined {
  const reported = disclosedReported(row, format);
  const parts = [row.sub, reported === undefined ? undefined : `reported ${format(reported)}`];
  const note = parts.filter((part): part is string => Boolean(part)).join(' · ');
  return note || undefined;
}

export function RankBar({
  rows,
  format,
  axisFormat,
  className,
  ariaLabel = 'Ranking chart',
  caption = 'Ranking',
}: {
  rows: RankRow[];
  format: (v: number) => string;
  /** Shorter formatter for x-axis ticks (tooltips keep `format`); avoids overlap on narrow screens. */
  axisFormat?: (v: number) => string;
  className?: string;
  /** The chart's accessible name; the default is the generic "Ranking chart". */
  ariaLabel?: string;
  /** The caption of the text alternative; the default is the generic "Ranking". */
  caption?: string;
}) {
  const t = useChartTokens();
  const hasStacked = rows.some((r) => r.segments && r.segments.length > 0);

  // ECharts category axis draws bottom-up; sort ascending so the largest
  // lands at the top via the natural order. Hoisted out of the option
  // builder so the text alternative lists the rows in the order the chart
  // draws them.
  const sorted = useMemo(
    // A row survives if EITHER number says it is real. A provider that
    // reports its own cost has no rate configured, so the segments sum to
    // zero while the reported total is not — filtering on the drawn sum
    // alone would drop the row that has a number to disclose.
    () =>
      [...rows]
        .filter((r) => Math.max(r.value, r.reportedValue ?? 0) > 0)
        .sort((a, b) => a.value - b.value),
    [rows],
  );

  const option = useMemo(() => {
    const categoryData = sorted.map((r) => r.label);
    const tickFormat = axisFormat ?? format;
    const axisStyle = baseAxisStyle(t);
    const xAxis = {
      type: 'value' as const,
      ...axisStyle,
      axisLabel: {
        ...axisStyle.axisLabel,
        hideOverlap: true,
        formatter: (v: number) => tickFormat(v),
      },
    };

    // ECharts truncates the label itself: it measures the text and cuts it to
    // the pixel budget, so the label adapts to the chart instead of being
    // sliced at a fixed character count.
    const yAxisLabel = {
      color: t.fgMuted,
      fontSize: 11,
      fontFamily: t.monoFamily,
      width: Y_AXIS_LABEL_WIDTH,
      overflow: 'truncate' as const,
      ellipsis: '…',
    };

    const yAxis = {
      type: 'category' as const,
      data: categoryData,
      ...baseAxisStyle(t),
      splitLine: { show: false },
      axisLabel: yAxisLabel,
    };

    if (!hasStacked) {
      // Simple single-value bars (legacy path).
      return {
        grid: { left: 8, right: 16, top: 8, bottom: 8, containLabel: true },
        tooltip: {
          ...baseTooltip(t),
          trigger: 'axis',
          axisPointer: { type: 'shadow' },
          formatter: (params: { name: string; value: number; dataIndex: number }[]) => {
            const p = params[0];
            const row = sorted[p.dataIndex];
            const lines = [`${p.name}: ${format(p.value)}`];
            const reported = row ? disclosedReported(row, format) : undefined;
            if (reported !== undefined) {
              lines.push(`<span style="opacity:.6">Reported: ${format(reported)}</span>`);
            }
            if (row?.sub) {
              lines.push(`<span style="opacity:.6">${row.sub}</span>`);
            }
            return lines.join('<br/>');
          },
        },
        xAxis,
        yAxis,
        series: [
          {
            type: 'bar',
            data: sorted.map((r) => r.value),
            barMaxWidth: 18,
            itemStyle: { color: t.accent, borderRadius: [0, 3, 3, 0] },
            emphasis: { itemStyle: { color: t.series[0] } },
          },
        ],
      };
    }

    // Stacked bars: collect all unique segment keys across rows.
    const segKeys = new Map<string, { label: string; color: string }>();
    for (const r of sorted) {
      for (const s of r.segments ?? []) {
        if (!segKeys.has(s.key)) segKeys.set(s.key, { label: s.label, color: s.color });
      }
    }
    const segmentDefs = Array.from(segKeys.entries()).map(([key, meta]) => ({ key, ...meta }));

    const series = segmentDefs.map((seg, i) => ({
      name: seg.label,
      type: 'bar' as const,
      stack: 'total',
      barMaxWidth: 18,
      data: sorted.map((r) => {
        const s = r.segments?.find((x) => x.key === seg.key);
        return s?.value ?? 0;
      }),
      itemStyle: {
        color: seg.color,
        borderRadius: i === segmentDefs.length - 1 ? [0, 3, 3, 0] : 0,
      },
    }));

    return {
      // The legend needs its own strip of grid: the stacked bars have no
      // visible segment names, so without it "is the green block Output or
      // Cache read?" is unanswerable — and on touch there is no hover
      // tooltip to fall back on. The strip is tall enough for the legend to
      // wrap to two lines on a narrow card without covering the x-axis
      // labels (one line ≈ 24px, two ≈ 48px).
      grid: { left: 8, right: 16, top: 8, bottom: 48, containLabel: true },
      legend: {
        bottom: 0,
        icon: 'square',
        itemWidth: 8,
        itemHeight: 8,
        textStyle: { color: t.fgMuted, fontSize: 11, fontFamily: t.fontFamily },
      },
      tooltip: {
        ...baseTooltip(t),
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: (params: { seriesName: string; value: number; dataIndex: number }[]) => {
          if (!params.length) return '';
          const idx = params[0].dataIndex;
          const r = sorted[idx];
          if (!r) return '';

          const lines: string[] = [`<b>${r.label}</b>`];
          for (const p of params) {
            const val = p.value as number;
            if (val > 0) {
              lines.push(`${p.seriesName}: ${format(val)}`);
            }
          }
          lines.push(`<span style="opacity:.6">Total: ${format(r.value)}</span>`);
          // The backend's authoritative total, when it disagrees with the sum
          // of the segments drawn above it.
          const reported = disclosedReported(r, format);
          if (reported !== undefined) {
            lines.push(`<span style="opacity:.6">Reported: ${format(reported)}</span>`);
          }
          if (r.sub) {
            lines.push(`<span style="opacity:.6">${r.sub}</span>`);
          }
          return lines.join('<br/>');
        },
      },
      xAxis,
      yAxis,
      series,
    };
  }, [sorted, format, axisFormat, t, hasStacked]);

  return (
    <>
      <EChart option={option} className={className ?? 'h-72'} ariaLabel={ariaLabel} />
      {/* The bar lengths and their segment split live only in the canvas and
          the hover tooltip, so they need a text alternative. */}
      <AccessibleSummary
        caption={caption}
        entries={sorted.map((r) => ({
          label: r.label,
          value: format(r.value),
          note: rowNote(r, format),
          details: r.segments
            ?.filter((s) => s.value > 0)
            .map((s) => ({ label: s.label, value: format(s.value) })),
        }))}
      />
    </>
  );
}
