import { ensureECharts } from '../charts.js';
import { fetchForecast } from '../api.js';

const STATUS_COLOR = {
    risk: 'var(--crit)',
    warn: 'var(--warn)',
    ok: 'var(--good)',
    stable: 'var(--accent)',
    insufficient_data: 'var(--text-dim)',
};

const FORECAST_CACHE_TTL_MS = 30_000;
let _forecastCache = null;
let _forecastCacheAt = 0;
let _forecastChart = null;

let _filterWindow = '';
let _filterProvider = '';

async function fetchForecastCached() {
    const now = Date.now();
    if (_forecastCache && (now - _forecastCacheAt) < FORECAST_CACHE_TTL_MS) {
        return _forecastCache;
    }
    const params = {};
    if (_filterWindow) params.window_type = _filterWindow;
    if (_filterProvider) params.provider_id = _filterProvider;
    const data = await fetchForecast(params);
    _forecastCache = data;
    _forecastCacheAt = now;
    return data;
}

function _confidenceLabel(confidence) {
    if (confidence >= 0.66) return 'high';
    if (confidence >= 0.33) return '~mid';
    return '?low';
}

function _renderKpi(summary) {
    const kpi = document.getElementById('forecast-kpi');
    if (!kpi) return;
    const items = [
        { label: 'RISK', key: 'risk', color: 'var(--crit)' },
        { label: 'WARN', key: 'warn', color: 'var(--warn)' },
        { label: 'OK', key: 'ok', color: 'var(--good)' },
        { label: 'STABLE', key: 'stable', color: 'var(--accent)' },
        { label: 'NO DATA', key: 'insufficient_data', color: 'var(--text-dim)' },
    ];
    kpi.innerHTML = items.map(({ label, key, color }) => `
        <div style="background:var(--surface);border:1px solid var(--hairline);padding:10px 18px;min-width:80px;text-align:center;">
            <div style="font-size:22px;font-weight:700;color:${color};font-family:'B612 Mono',monospace;">${summary[key] ?? 0}</div>
            <div style="font-size:9px;color:var(--text-dim);letter-spacing:0.12em;margin-top:2px;">${label}</div>
        </div>
    `).join('');
}

function _renderTable(forecasts) {
    const tbody = document.getElementById('forecast-table-body');
    const empty = document.getElementById('forecast-empty');
    if (!tbody) return;

    const sorted = [...forecasts].sort((a, b) => (b.projected_pct ?? -1) - (a.projected_pct ?? -1));

    if (sorted.length === 0) {
        tbody.innerHTML = '';
        empty?.classList.remove('hidden');
        return;
    }
    empty?.classList.add('hidden');

    tbody.innerHTML = sorted.map(f => {
        const color = STATUS_COLOR[f.status] || 'var(--text-dim)';
        const nowPct = f.now_pct != null ? f.now_pct.toFixed(1) + '%' : '—';
        const projPct = (f.status === 'stable' || f.projected_pct == null) ? '—' : f.projected_pct.toFixed(1) + '%';
        const conf = _confidenceLabel(f.confidence);
        const resetDate = f.reset_at ? new Date(f.reset_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : '—';
        const label = f.service_name || f.provider_id;
        return `<tr style="border-bottom:1px solid var(--hairline);color:var(--text);">
            <td style="padding:6px 8px;">${label}</td>
            <td style="padding:6px 8px;color:var(--text-dim);">${f.provider_id}</td>
            <td style="padding:6px 8px;text-align:right;">${nowPct}</td>
            <td style="padding:6px 8px;text-align:right;color:${color};font-weight:700;">${projPct}</td>
            <td style="padding:6px 8px;text-align:right;color:var(--text-dim);">${conf}</td>
            <td style="padding:6px 8px;color:var(--text-dim);">${resetDate}</td>
            <td style="padding:6px 8px;"><span style="color:${color};text-transform:uppercase;letter-spacing:0.08em;">${f.status}</span></td>
        </tr>`;
    }).join('');
}

async function _renderChart(forecasts) {
    await ensureECharts();
    
    const el = document.getElementById('forecast-chart');
    if (!el || typeof echarts === 'undefined') return;

    if (_forecastChart) {
        _forecastChart.dispose();
        _forecastChart = null;
    }

    // Only chart forecasts with meaningful projections (exclude stable/no-data noise)
    const chartable = forecasts.filter(
        f => f.projected_pct != null && f.now_pct != null
            && f.status !== 'stable' && f.status !== 'insufficient_data'
    );
    if (chartable.length === 0) {
        el.style.display = 'none';
        return;
    }
    el.style.display = '';

    const css = getComputedStyle(document.documentElement);
    const cSurface = css.getPropertyValue('--surface').trim() || '#1a1a2e';
    const cText = css.getPropertyValue('--text').trim() || '#e0e0e0';
    const cTextDim = css.getPropertyValue('--text-dim').trim() || '#666';
    const cHairline = css.getPropertyValue('--hairline').trim() || '#2a2a3e';
    const cCrit = css.getPropertyValue('--crit').trim() || '#ff4444';
    const cWarn = css.getPropertyValue('--warn').trim() || '#ffaa00';
    const cGood = css.getPropertyValue('--good').trim() || '#00cc88';

    // Build bar chart: now_pct (solid) + delta to projected_pct (dashed stack)
    const labels = chartable.map(f => f.service_name || f.provider_id);
    const nowData = chartable.map(f => parseFloat((f.now_pct ?? 0).toFixed(1)));
    const projData = chartable.map(f => parseFloat((f.projected_pct ?? 0).toFixed(1)));

    const barColors = chartable.map(f => {
        if (f.status === 'risk') return cCrit;
        if (f.status === 'warn') return cWarn;
        return cGood;
    });

    _forecastChart = echarts.init(el);
    _forecastChart.setOption({
        backgroundColor: 'transparent',
        tooltip: {
            trigger: 'axis',
            backgroundColor: cSurface,
            borderColor: cHairline,
            borderWidth: 1,
            padding: [8, 12],
            textStyle: { color: cText, fontFamily: 'B612 Mono, monospace', fontSize: 10 },
            formatter: (params) => {
                const label = params[0]?.name ?? '';
                const lines = params.map(p => `<div>${p.seriesName}: <b>${p.value}%</b></div>`).join('');
                return `<div style="color:${cTextDim};font-size:9px;margin-bottom:4px;">${label}</div>${lines}`;
            }
        },
        grid: { top: 20, left: 60, right: 20, bottom: 40, containLabel: false },
        xAxis: {
            type: 'category',
            data: labels,
            axisLabel: { color: cTextDim, fontSize: 9, fontFamily: 'B612 Mono, monospace', rotate: labels.length > 4 ? 30 : 0 },
            axisLine: { lineStyle: { color: cHairline } },
            axisTick: { show: false }
        },
        yAxis: {
            type: 'value',
            name: '%',
            min: 0,
            nameTextStyle: { color: cTextDim, fontSize: 9, fontFamily: 'B612 Mono, monospace' },
            axisLabel: { color: cTextDim, fontSize: 9, fontFamily: 'B612 Mono, monospace', formatter: v => v + '%' },
            splitLine: { lineStyle: { color: cHairline, type: 'dashed' } },
            axisLine: { show: false },
            markLine: { data: [{ yAxis: 100, lineStyle: { color: cCrit, type: 'dashed', width: 1 } }] }
        },
        series: [
            {
                name: 'Current %',
                type: 'bar',
                data: nowData,
                itemStyle: { color: (params) => barColors[params.dataIndex] + '88' },
                barMaxWidth: 40,
            },
            {
                name: 'Projected %',
                type: 'bar',
                data: projData,
                itemStyle: { color: (params) => barColors[params.dataIndex], borderRadius: [2, 2, 0, 0] },
                barMaxWidth: 40,
                barGap: '-100%',
                z: 2,
                opacity: 0.5,
            }
        ]
    });
}

function _populateProviderFilter(forecasts) {
    if (_filterProvider) return;  // keep full list while a filter is active
    const sel = document.getElementById('forecast-filter-provider');
    if (!sel) return;
    const providers = [...new Set(forecasts.map(f => f.provider_id))].sort();
    const current = sel.value;
    sel.innerHTML = '<option value="">All providers</option>' +
        providers.map(p => `<option value="${p}"${p === current ? ' selected' : ''}>${p}</option>`).join('');
}

export async function loadForecastView() {
    try {
        const data = await fetchForecastCached();
        const forecasts = data.forecasts ?? [];
        const summary = data.summary ?? {};

        _renderKpi(summary);
        _populateProviderFilter(forecasts);
        _renderTable(forecasts);
        await _renderChart(forecasts);

        const genAt = document.getElementById('forecast-generated-at');
        if (genAt && data.generated_at) {
            genAt.textContent = 'Generated: ' + new Date(data.generated_at).toLocaleTimeString();
        }
    } catch (err) {
        console.error('Failed to load forecast view:', err);
        const tbody = document.getElementById('forecast-table-body');
        if (tbody) tbody.innerHTML = `<tr><td colspan="7" style="padding:20px;color:var(--crit);text-align:center;">Failed to load forecast data.</td></tr>`;
    }
}

export function initForecastView() {
    const windowSel = document.getElementById('forecast-filter-window');
    const providerSel = document.getElementById('forecast-filter-provider');

    if (windowSel) {
        windowSel.addEventListener('change', () => {
            _filterWindow = windowSel.value;
            _forecastCache = null; // invalidate cache on filter change
            loadForecastView();
        });
    }
    if (providerSel) {
        providerSel.addEventListener('change', () => {
            _filterProvider = providerSel.value;
            _forecastCache = null;
            loadForecastView();
        });
    }
}
