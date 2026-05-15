// frontend/js/charts.js
// Apache ECharts wrapper for the History tab usage panel.
// ECharts is lazy-loaded on first use of the History view.

import { getUserTz } from './utils/tz.js';

let _chart = null;
let _echarts = null;
let _legendState = {};

// Registered once at module load; stays alive for the lifetime of the page.
window.addEventListener('resize', () => _chart && _chart.resize());

const PROVIDER_COLORS = {
    anthropic: "#f59e0b",
    gemini: "#3b82f6",
    github: "#8b5cf6",
    chatgpt: "#10b981",
    opencode: "#06b6d4",
    openrouter: "#ec4899",
    minimax: "#14b8a6",
    ollama: "#94a3b8",
};

/**
 * Get base color for a provider.
 */
function colorFor(providerId) {
    return PROVIDER_COLORS[providerId] || "#64748b";
}

/**
 * Generate a shade/style for a specific series within a provider.
 * Uses different opacity and dash patterns to distinguish metrics.
 */
function getSeriesStyle(providerId, index) {
    const baseColor = colorFor(providerId);
    // Patterns for different lines of the same provider
    const patterns = [
        { type: 'solid', dash: null },
        { type: 'dashed', dash: [5, 5] },
        { type: 'dotted', dash: [2, 2] },
        { type: 'dash-dot', dash: [10, 2, 2, 2] }
    ];
    const pattern = patterns[index % patterns.length];
    return {
        color: baseColor,
        lineType: pattern.type,
        lineDash: pattern.dash
    };
}

/**
 * Pick bucket granularity for a given window. Mirrors backend _pick_bucket_seconds.
 */
export function pickBucketSeconds(days) {
    if (days >= 30) return 86400;   // 30d/90d → daily      (~30–90 pts)
    if (days >= 7)  return 10800;   // 7d → 3-hourly        (~56 pts)
    if (days >= 1)  return 1800;    // 24h → 30-min         (~48 pts)
    if (days >= 0.25) return 900;   // 6h → 15-min          (~24 pts)
    return 300;                     // 1h → 5-min            (~12 slots)
}

export function bucketKeyFor(isoTs, bucketSeconds) {
    const t = Math.floor(new Date(isoTs).getTime() / 1000);
    return t - (t % bucketSeconds);
}

export function formatBucketLabel(bucketEpoch, bucketSeconds) {
    const d = new Date(bucketEpoch * 1000);
    const tz = getUserTz();
    if (bucketSeconds >= 86400) {
        return d.toLocaleDateString([], { month: 'short', day: 'numeric', timeZone: tz });
    }
    if (bucketSeconds >= 3600) {
        return d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', timeZone: tz });
    }
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', timeZone: tz });
}

/**
 * Identify all unique series combinations in the dataset.
 */
function extractSeriesKeys(snapshots) {
    const keys = new Set();
    for (const s of snapshots) {
        const key = `${s.provider_id || 'unknown'}|${s.service_name || 'Usage'}|${s.window_type || 'unknown'}|${s.variant || ''}|${s.model_id || ''}`;
        keys.add(key);
    }
    return Array.from(keys).sort();
}

/**
 * Bucket snapshots by (bucket, seriesKey) at the given granularity.
 * Tracks both average (sum/count) and max for peak display.
 */
function bucketByMetric(snapshots, metric, bucketSeconds) {
    const buckets = {};
    for (const snap of snapshots) {
        let value;
        let maxValue;
        if (metric === 'cost') {
            if (snap.unit_type !== 'currency' || snap.used_value == null) continue;
            value = snap.used_value;
            maxValue = snap.max_used_value != null ? snap.max_used_value : value;
        } else if (metric === 'tokens') {
            // Use token_usage.total from backend (populated by poller from card.token_usage)
            if (snap.token_usage?.total != null) {
                value = snap.token_usage.total;
            } else if (snap.unit_type === 'tokens' && snap.used_value != null) {
                value = snap.used_value;
            } else {
                continue;
            }
            maxValue = value; // No max for tokens currently
        } else {
            if (snap.unit_type === 'percent' && snap.used_value != null) {
                value = snap.used_value;
            } else if (['tokens', 'requests', 'messages', 'credits'].includes(snap.unit_type)
                       && snap.used_value != null && snap.limit_value > 0) {
                value = (snap.used_value / snap.limit_value) * 100;
            } else {
                continue;
            }
            // Compute peak in the same unit as value, using server-provided max_used_value when available
            maxValue = value;
            if (snap.max_used_value != null) {
                const rawMax = snap.max_used_value;
                if (snap.unit_type === 'percent') maxValue = rawMax;
                else if (snap.limit_value > 0) maxValue = (rawMax / snap.limit_value) * 100;
            }
        }

        const bucket = bucketKeyFor(snap.timestamp, bucketSeconds);
        const key = `${snap.provider_id || 'unknown'}|${snap.service_name || 'Usage'}|${snap.window_type || 'unknown'}|${snap.variant || ''}|${snap.model_id || ''}`;

        if (!buckets[bucket]) buckets[bucket] = {};
        if (!buckets[bucket][key]) buckets[bucket][key] = { sum: 0, count: 0, max: -Infinity };
        buckets[bucket][key].sum += value;
        buckets[bucket][key].count += 1;
        if (maxValue != null) {
            buckets[bucket][key].max = Math.max(buckets[bucket][key].max, maxValue);
        }
    }
    return buckets;
}

export function destroyCharts() {
    if (_chart) {
        _chart.dispose();
        _chart = null;
    }
}

export async function ensureECharts() {
    if (_echarts) return _echarts;
    if (window.echarts) {
        _echarts = window.echarts;
        return _echarts;
    }
    
    // Check if it was already loaded via script tag in index.html or needs dynamic loading
    if (!window.echarts) {
        await new Promise((resolve, reject) => {
            const script = document.createElement('script');
            script.src = '/static/js/lib/echarts.min.js';
            script.onload = () => resolve();
            script.onerror = () => reject(new Error('Failed to load ECharts'));
            document.head.appendChild(script);
        });
    }
    _echarts = window.echarts;
    return _echarts;
}

/**
 * @param {Array} snapshots - history snapshot objects
 * @param {'percent'|'tokens'|'cost'} [metric='percent'] - which value to plot
 * @param {number} [days=7] - active history window
 * @param {string} [windowFilter='all'] - optional filter for window_type
 * @param {boolean} [showPeaks=false] - show peak values instead of averages
 */
export async function updateCharts(snapshots, metric = 'percent', days = 7, windowFilter = 'all', showPeaks = false) {
    const container = document.getElementById("chart-usage");
    const emptyEl = document.getElementById("chart-empty");
    if (!container) return;

    // Filter by window_type if requested
    let filteredSnapshots = snapshots;
    if (windowFilter !== 'all') {
        filteredSnapshots = snapshots.filter(s => s.window_type === windowFilter);
    }

    if (!filteredSnapshots || filteredSnapshots.length === 0) {
        emptyEl?.classList.remove("hidden");
        document.getElementById("chart-wrap")?.classList.add("hidden");
        return;
    }
    emptyEl?.classList.add("hidden");
    document.getElementById("chart-wrap")?.classList.remove("hidden");

    const bucketSeconds = pickBucketSeconds(days);
    const seriesKeys = extractSeriesKeys(filteredSnapshots);
    const buckets = bucketByMetric(filteredSnapshots, metric, bucketSeconds);

    // Get sorted bucket epochs for the X axis
    const bucketEpochs = Object.keys(buckets).map(Number).sort((a, b) => a - b);
    if (bucketEpochs.length === 0) {
        emptyEl?.classList.remove("hidden");
        document.getElementById("chart-wrap")?.classList.add("hidden");
        return;
    }

    const xAxisData = bucketEpochs.map(e => formatBucketLabel(e, bucketSeconds));
    const yLabel = metric === 'cost' ? 'Cost (USD)' : metric === 'tokens' ? 'Tokens' : '% Used';

    // Grouping series by provider for color stability
    const providerCounts = {};

    // Build series: averages always, BAND mode adds min-max shaded area (not doubled series)
    let series = [];

    // First pass: averages (always shown)
    const avgSeries = seriesKeys.map(key => {
        const [pid, , wtype, variant, modelId] = key.split('|');
        const subParts = [];
        if (variant) subParts.push(variant);
        if (modelId) subParts.push(modelId.replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase()));
        if (wtype && wtype !== 'unknown' && wtype !== 'rolling') subParts.push(wtype.charAt(0).toUpperCase() + wtype.slice(1));
        const displayName = subParts.join(' · ');
        if (providerCounts[pid] === undefined) providerCounts[pid] = 0;
        const style = getSeriesStyle(pid, providerCounts[pid]++);

        return {
            name: displayName ? `${pid.toUpperCase()}: ${displayName}` : pid.toUpperCase(),
            type: 'line',
            smooth: true,
            symbol: 'circle',
            symbolSize: 4,
            showSymbol: bucketEpochs.length < 100,
            lineStyle: {
                width: 2,
                type: style.lineType,
                dashOffset: 0,
            },
            itemStyle: { color: style.color },
            areaStyle: {
                color: {
                    type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
                    colorStops: [
                        { offset: 0, color: style.color + '33' },
                        { offset: 1, color: style.color + '00' }
                    ]
                }
            },
            data: bucketEpochs.map(ep => {
                const b = buckets[ep]?.[key];
                return b ? parseFloat((b.sum / b.count).toFixed(2)) : null;
            }),
            connectNulls: true,
            emphasis: {
                focus: 'series',
                lineStyle: { width: 3 }
            },
            z: 2
        };
    });

    series = avgSeries;

    // BAND mode: render a single min-max shaded band per series (not doubled legend items)
    if (showPeaks) {
        const bandCounts = {};
        const bandSeries = seriesKeys.flatMap(key => {
            const [pid] = key.split('|');
            if (bandCounts[pid] === undefined) bandCounts[pid] = 0;
            const style = getSeriesStyle(pid, bandCounts[pid]++);
            const alpha = '28'; // ~16% opacity for band fill

            // Lower bound — hidden line at avg, basis for band
            const minLine = {
                name: `__band_min_${key}`,
                type: 'line',
                smooth: true,
                symbol: 'none',
                lineStyle: { opacity: 0 },
                itemStyle: { color: style.color },
                areaStyle: { color: 'transparent' },
                data: bucketEpochs.map(ep => {
                    const b = buckets[ep]?.[key];
                    return b ? parseFloat((b.sum / b.count).toFixed(2)) : null;
                }),
                connectNulls: true,
                stack: `band_${key}`,
                z: 0,
                legendHoverLink: false,
                tooltip: { show: false },
            };
            // Upper bound (max - min) on top of minLine forms the band
            const bandFill = {
                name: `__band_fill_${key}`,
                type: 'line',
                smooth: true,
                symbol: 'none',
                lineStyle: { opacity: 0 },
                itemStyle: { color: style.color },
                areaStyle: { color: style.color + alpha },
                data: bucketEpochs.map(ep => {
                    const b = buckets[ep]?.[key];
                    if (!b) return null;
                    const avg = b.sum / b.count;
                    // Band width: max minus avg (so center line = avg series, band extends to max)
                    return parseFloat((b.max - avg).toFixed(2));
                }),
                connectNulls: true,
                stack: `band_${key}`,
                z: 0,
                legendHoverLink: false,
                tooltip: { show: false },
            };
            return [minLine, bandFill];
        });
        series = [...bandSeries, ...series]; // bands behind avg lines
    }

    try {
        const echarts = await ensureECharts();
        if (!_chart) {
            _chart = echarts.init(container);
        }

        const css = getComputedStyle(document.documentElement);
        const cText     = css.getPropertyValue('--text').trim()       || '#e8e4d4';
        const cTextDim  = css.getPropertyValue('--text-dim').trim()   || '#5a6068';
        const cHairline = css.getPropertyValue('--hairline').trim()   || '#1e2630';
        const cSurface  = css.getPropertyValue('--surface').trim()    || '#0f1216';
        const cAccent   = css.getPropertyValue('--accent').trim()     || '#ffb000';
        const cAccentCl = css.getPropertyValue('--accent-cool').trim()|| '#00d4ff';

        const option = {
            backgroundColor: 'transparent',
            tooltip: {
                trigger: 'item',
                backgroundColor: cSurface,
                borderColor: cHairline,
                borderWidth: 1,
                padding: [10, 15],
                textStyle: { color: cText, fontFamily: 'B612 Mono, monospace', fontSize: 11 },
                formatter: (params) => {
                    const unit = metric === 'percent' ? '%' : (metric === 'cost' ? ' USD' : '');
                    const val = metric === 'percent'
                        ? params.value.toFixed(1)
                        : Number(params.value).toLocaleString();
                    return `
                        <div style="margin-bottom: 4px; color: ${cTextDim}; font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em;">${params.name}</div>
                        <div style="display: flex; align-items: center; gap: 8px;">
                            <span style="display: inline-block; width: 6px; height: 6px; background-color: ${params.color}"></span>
                            <span style="font-weight: 700; color: ${cText};">${params.seriesName}</span>
                            <span style="margin-left: 12px; font-family: 'B612 Mono', monospace; color: ${cAccent};">${val}${unit}</span>
                        </div>
                    `;
                }
            },
            legend: {
                show: false,
                data: series.map(s => s.name),
                selected: { ..._legendState }
            },
            grid: {
                top: 40,
                left: 60,
                right: 30,
                bottom: 50,
                containLabel: true
            },
            xAxis: {
                type: 'category',
                boundaryGap: false,
                data: xAxisData,
                axisLabel: { color: cTextDim, fontSize: 9, margin: 15, fontFamily: 'B612 Mono, monospace' },
                axisLine: { lineStyle: { color: cHairline } },
                axisTick: { show: false }
            },
            yAxis: {
                type: 'value',
                name: yLabel,
                nameTextStyle: { color: cTextDim, fontSize: 9, align: 'right', fontFamily: 'B612 Mono, monospace' },
                axisLabel: { color: cTextDim, fontSize: 9, fontFamily: 'B612 Mono, monospace' },
                splitLine: { lineStyle: { color: cHairline, type: 'dashed' } },
                axisLine: { show: false }
            },
            dataZoom: [
                { type: 'inside', start: 0, end: 100 },
                {
                    type: 'slider',
                    bottom: 10,
                    height: 20,
                    borderColor: 'transparent',
                    fillerColor: `${cAccentCl}1a`,
                    handleIcon: 'path://M10.7,11.9v-1.3H9.3v1.3c-4.9,0.3-8.8,4.4-8.8,9.4c0,5,3.9,9.1,8.8,9.4v1.3h1.3v-1.3c4.9-0.3,8.8-4.4,8.8-9.4C19.5,16.3,15.6,12.2,10.7,11.9z M13.3,24.4H6.7V23h6.6V24.4z M13.3,19.6H6.7v-1.4h6.6V19.6z',
                    handleSize: '80%',
                    handleStyle: { color: cHairline },
                    textStyle: { color: 'transparent' },
                    dataBackground: {
                        lineStyle: { color: cAccentCl, opacity: 0.2 },
                        areaStyle: { color: cAccentCl, opacity: 0.1 }
                    }
                }
            ],
            series: series
        };
        
        _chart.setOption(option, true);  // true = notMerge: replace all series/axes cleanly

        // Prune stale legend state entries, then render the HTML legend.
        const names = new Set(series.map(s => s.name));
        for (const k of Object.keys(_legendState)) if (!names.has(k)) delete _legendState[k];
        _renderHtmlLegend(series);

    } catch (err) {
        console.error('Failed to init ECharts:', err);
        emptyEl.textContent = 'Failed to load chart. Please refresh.';
        emptyEl?.classList.remove("hidden");
    }
}

function _renderHtmlLegend(series) {
    const host = document.getElementById('chart-legend');
    if (!host) return;
    host.innerHTML = series
        .filter(s => s.name && s.legendHoverLink !== false)
        .map(s => {
            const on = _legendState[s.name] !== false;
            const color = s.itemStyle?.color || '#64748b';
            const safeName = s.name.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
            return `<span class="chart-legend-item${on ? '' : ' muted'}" data-name="${safeName}">` +
                `<span class="chart-legend-swatch" style="background:${color}"></span>${safeName}</span>`;
        }).join('');
    host.onclick = (e) => {
        const item = e.target.closest('.chart-legend-item');
        if (!item || !_chart) return;
        const name = item.dataset.name;
        _legendState[name] = _legendState[name] === false;
        item.classList.toggle('muted', _legendState[name] === false);
        _chart.setOption({ legend: { selected: { ..._legendState } } });
    };
}
