// frontend/js/components/forecast-trajectory.js
// Shared mini-chart renderer for per-pool cumulative-pct trajectory.
// Extracted from views/forecast.js so the provider modal can reuse it.

import { ensureECharts } from '../charts.js';
import { formatLocalTime, formatLocalDate } from '../utils/tz.js';

export const STATUS_COLOR = {
    exhausted: 'var(--crit)',
    near_limit: 'var(--crit)',
    risk: 'var(--crit)',
    warn: 'var(--warn)',
    decelerating: 'var(--info, #4a9eff)',
    ok: 'var(--good)',
    stable: 'var(--accent)',
    insufficient_data: 'var(--text-dim)',
};

function _shortModelName(modelId) {
    if (!modelId) return null;
    const m = modelId.toLowerCase();
    if (m.includes('sonnet')) return 'Sonnet';
    if (m.includes('opus'))   return 'Opus';
    if (m.includes('haiku'))  return 'Haiku';
    // strip "claude-" prefix as a generic fallback
    return modelId.replace(/^claude-/i, '');
}

/** Format the header subtitle for a forecast entry's trajectory chart. */
export function formatTrajectoryHeader(entry) {
    const winMap = { session: 'Session', daily: 'Daily', weekly: 'Weekly', monthly: 'Monthly' };
    const win = winMap[entry.window_type] || entry.window_type || '';
    const modelName = _shortModelName(entry.model_id);
    const svc = entry.service_name || entry.provider_id || '';
    const modelPart = modelName && !svc.toLowerCase().includes(modelName.toLowerCase()) ? modelName : null;
    const label = [svc, modelPart, win].filter(Boolean).join(' · ');

    let detail = '';
    if (entry.projected_limit_hit_at) {
        const d = formatLocalDate(entry.projected_limit_hit_at);
        const t = formatLocalTime(entry.projected_limit_hit_at);
        detail = `hits 100% ${d} ${t}`;
    } else if (entry.projected_pct != null) {
        detail = `${entry.projected_pct.toFixed(1)}% projected`;
    }
    return { label, detail };
}

/**
 * Render an ECharts trajectory mini-chart into `containerEl`.
 * `seriesPoints` is the `series` array from a ForecastEntry with `include_series=true`.
 * `windowStart` / `resetAt` pin the x-axis to the full window.
 * `projectedPct` / `projectedLimitHitAt` extend the chart with a dashed projection line.
 * Returns the echarts instance (caller should dispose when container is removed).
 */
export async function renderTrajectoryChart(containerEl, seriesPoints, {
    windowStart, resetAt, projectedPct, projectedLimitHitAt,
} = {}) {
    if (!containerEl) return null;
    await ensureECharts();
    if (typeof echarts === 'undefined') return null;

    if (!Array.isArray(seriesPoints) || seriesPoints.length === 0) {
        containerEl.innerHTML = '<div style="padding:24px;text-align:center;color:var(--text-dim);font-size:11px;">No bucket data available.</div>';
        return null;
    }

    containerEl.innerHTML = '';
    const chart = echarts.init(containerEl);
    const css = getComputedStyle(document.documentElement);
    const cText     = css.getPropertyValue('--text').trim()     || '#e0e0e0';
    const cTextDim  = css.getPropertyValue('--text-dim').trim() || '#666';
    const cHairline = css.getPropertyValue('--hairline').trim() || '#2a2a3e';
    const cAccent   = css.getPropertyValue('--accent').trim()   || '#4a9eff';

    const points = seriesPoints.map(p => [Date.parse(p.ts), p.pct]);
    const xMin = windowStart ? Date.parse(windowStart) : undefined;
    const xMax = resetAt    ? Date.parse(resetAt)      : undefined;

    // Build dashed projection segment: last actual point → projected endpoint
    const projPoints = [];
    const lastPt = points[points.length - 1];
    if (lastPt) {
        const projX = projectedLimitHitAt
            ? Date.parse(projectedLimitHitAt)
            : (resetAt ? Date.parse(resetAt) : null);
        const projY = projectedLimitHitAt ? 100 : projectedPct;
        if (projX != null && projY != null && projX > lastPt[0]) {
            projPoints.push([lastPt[0], lastPt[1]]);
            projPoints.push([projX, Math.min(projY, 100)]);
        }
    }

    chart.setOption({
        backgroundColor: 'transparent',
        grid: { top: 10, left: 40, right: 10, bottom: 20, containLabel: false },
        tooltip: {
            trigger: 'axis',
            formatter: (params) => {
                const p = params[0];
                const ts = new Date(p.value[0]).toLocaleString();
                return `<div style="font-size:10px;color:${cText};">${ts}<br><b>${p.value[1].toFixed(2)}%</b></div>`;
            },
        },
        xAxis: {
            type: 'time',
            min: xMin,
            max: xMax,
            axisLine: { lineStyle: { color: cHairline } },
            axisLabel: { color: cTextDim, fontSize: 9, fontFamily: 'B612 Mono, monospace' },
        },
        yAxis: {
            type: 'value',
            min: 0,
            max: 100,
            splitLine: { lineStyle: { color: cHairline, type: 'dashed' } },
            axisLabel: { color: cTextDim, fontSize: 9, formatter: v => v + '%' },
            axisLine: { show: false },
        },
        series: [
            {
                type: 'line',
                data: points,
                symbol: 'circle',
                symbolSize: 4,
                lineStyle: { color: cAccent, width: 1.5 },
                itemStyle: { color: cAccent },
                areaStyle: { color: cAccent, opacity: 0.08 },
            },
            ...(projPoints.length ? [{
                type: 'line',
                data: projPoints,
                symbol: 'none',
                lineStyle: { color: cAccent, width: 1.5, type: 'dashed', opacity: 0.5 },
                itemStyle: { color: cAccent },
                areaStyle: { color: cAccent, opacity: 0.03 },
            }] : []),
        ],
    });
    return chart;
}
