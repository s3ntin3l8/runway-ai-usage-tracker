import { ensureECharts } from '../charts.js';
import { fetchForecast } from '../api.js';

const STATUS_COLOR = {
    exhausted: 'var(--crit)',
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

const _MODEL_DISPLAY = {
    'sonnet': 'Sonnet', 'opus': 'Opus', 'haiku': 'Haiku',
    'design': 'Design', 'flash': 'Flash', 'pro': 'Pro', 'flash-lite': 'Flash Lite',
};
const _WINDOW_DISPLAY = {
    'session': 'Session', 'daily': 'Daily', 'weekly': 'Weekly', 'monthly': 'Monthly',
};

function _forecastSubtitle(entry) {
    const parts = [];
    if (entry.variant) parts.push(String(entry.variant));
    if (entry.model_id) {
        parts.push(_MODEL_DISPLAY[entry.model_id] || String(entry.model_id).replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase()));
    }
    const w = _WINDOW_DISPLAY[entry.window_type];
    if (w) parts.push(w);
    return parts.join(' · ');
}

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
        { label: 'EXHAUSTED', key: 'exhausted', color: 'var(--crit)' },
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

        let projPct = (f.status === 'stable' || f.status === 'exhausted' || f.projected_pct == null) ? '—' : f.projected_pct.toFixed(1) + '%';
        if (f.projected_limit_hit_at) {

            const hitAt = new Date(f.projected_limit_hit_at);
            const timeStr = hitAt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            const dateStr = hitAt.toLocaleDateString([], { month: 'short', day: 'numeric' });
            projPct = `100% (${dateStr} ${timeStr})`;
        }

        const conf = _confidenceLabel(f.confidence);
        const resetDate = f.reset_at ? new Date(f.reset_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : '—';
        const baseLabel = f.service_name || f.provider_id;
        const sub = _forecastSubtitle(f);
        const label = sub ? `${baseLabel} · ${sub}` : baseLabel;
        return `<tr>
            <td>${label}</td>
            <td>${f.provider_id}</td>
            <td class="num">${nowPct}</td>
            <td class="num ht-bold" style="color:${color};">${projPct}</td>
            <td class="num ht-italic">${conf}</td>
            <td>${resetDate}</td>
            <td><span style="color:${color};text-transform:uppercase;letter-spacing:0.08em;">${f.status}</span></td>
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
    const labels = chartable.map(f => {
        const base = f.service_name || f.provider_id;
        const sub = _forecastSubtitle(f);
        return sub ? `${base} · ${sub}` : base;
    });
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
                const name = params[0].name;
                const nowVal = params[0].value;
                const projVal = params[1].value;
                const f = chartable[params[0].dataIndex];

                let t = `<div style="font-weight:700;margin-bottom:8px;">${name}</div>`;
                t += `<div>Current: <span style="font-weight:600;">${nowVal}%</span></div>`;

                if (f.projected_limit_hit_at) {
                    const hitAt = new Date(f.projected_limit_hit_at);
                    const timeStr = hitAt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
                    const dateStr = hitAt.toLocaleDateString([], { month: 'short', day: 'numeric' });
                    t += `<div style="margin-top:4px;">Projected: <span style="font-weight:600;color:var(--crit)">Hits 100% on ${dateStr} at ${timeStr}</span></div>`;
                } else {
                    t += `<div style="margin-top:4px;">Projected: <span style="font-weight:600;">${projVal}%</span></div>`;
                }
                return t;
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

function _renderWindowChips() {
    const el = document.getElementById('forecast-window-chips');
    if (!el) return;
    const windows = [
        { val: '', label: 'All windows' },
        { val: 'daily', label: 'Daily' },
        { val: 'weekly', label: 'Weekly' },
        { val: 'biweekly', label: 'Biweekly' },
        { val: 'monthly', label: 'Monthly' }
    ];
    el.innerHTML = windows.map(w => {
        const active = _filterWindow === w.val ? ' active' : '';
        return `<button class="chip${active}" data-window="${w.val}">${w.label}</button>`;
    }).join('');
}

function _populateProviderFilter(forecasts) {
    const el = document.getElementById('forecast-provider-chips');
    if (!el) return;
    
    // Always recalculate unique providers from the current data
    const providers = [...new Set(forecasts.map(f => f.provider_id))].sort();
    
    let html = `<button class="chip${_filterProvider === '' ? ' active' : ''}" data-prov="">All providers</button>`;
    html += providers.map(p => {
        const active = _filterProvider === p ? ' active' : '';
        return `<button class="chip${active}" data-prov="${p}">${p}</button>`;
    }).join('');
    
    el.innerHTML = html;
}

export async function loadForecastView() {
    try {
        const data = await fetchForecastCached();
        const forecasts = data.forecasts ?? [];
        const summary = data.summary ?? {};

        _renderKpi(summary);
        _renderWindowChips();
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
        if (tbody) tbody.innerHTML = `<tr><td colspan="7" class="ht-empty">Failed to load forecast data.</td></tr>`;
    }
}

export function initForecastView() {
    const windowChips = document.getElementById('forecast-window-chips');
    const providerChips = document.getElementById('forecast-provider-chips');

    if (windowChips) {
        windowChips.addEventListener('click', (e) => {
            const btn = e.target.closest('.chip');
            if (!btn) return;
            _filterWindow = btn.dataset.window || '';
            _forecastCache = null;
            loadForecastView();
        });
    }

    if (providerChips) {
        providerChips.addEventListener('click', (e) => {
            const btn = e.target.closest('.chip');
            if (!btn) return;
            _filterProvider = btn.dataset.prov || '';
            _forecastCache = null;
            loadForecastView();
        });
    }
}
