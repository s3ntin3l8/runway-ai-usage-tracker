// frontend/js/charts.js
// Apache ECharts wrapper for the History tab usage panel.
// ECharts is lazy-loaded on first use of the History view.

let _chart = null;
let _echarts = null;

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
    if (days >= 2) return 86400;  // 7d/30d/90d → daily
    if (days >= 0.5) return 3600; // 1d → hourly
    if (days >= 0.1) return 900;  // 6h → 15 min
    return 60;                     // 1h → 1 min
}

export function bucketKeyFor(isoTs, bucketSeconds) {
    const t = Math.floor(new Date(isoTs).getTime() / 1000);
    return t - (t % bucketSeconds);
}

export function formatBucketLabel(bucketEpoch, bucketSeconds) {
    const d = new Date(bucketEpoch * 1000);
    if (bucketSeconds >= 86400) {
        return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
    }
    if (bucketSeconds >= 3600) {
        return d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit' });
    }
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

/**
 * Identify all unique series combinations in the dataset.
 */
function extractSeriesKeys(snapshots) {
    const keys = new Set();
    for (const s of snapshots) {
        const key = `${s.provider_id || 'unknown'}|${s.service_name || 'Usage'}|${s.window_type || 'unknown'}`;
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
        if (metric === 'cost') {
            if (snap.unit_type !== 'currency' || snap.used_value == null) continue;
            value = snap.used_value;
        } else if (metric === 'tokens') {
            if (snap.unit_type !== 'tokens' || snap.used_value == null) continue;
            value = snap.used_value;
        } else {
            if (snap.unit_type === 'percent' && snap.used_value != null) {
                value = snap.used_value;
            } else if (['tokens', 'requests', 'messages', 'credits'].includes(snap.unit_type)
                       && snap.used_value != null && snap.limit_value > 0) {
                value = (snap.used_value / snap.limit_value) * 100;
            } else {
                continue;
            }
        }
        const bucket = bucketKeyFor(snap.timestamp, bucketSeconds);
        const key = `${snap.provider_id || 'unknown'}|${snap.service_name || 'Usage'}|${snap.window_type || 'unknown'}`;

        if (!buckets[bucket]) buckets[bucket] = {};
        if (!buckets[bucket][key]) buckets[bucket][key] = { sum: 0, count: 0, max: -Infinity };
        buckets[bucket][key].sum += value;
        buckets[bucket][key].count += 1;
        buckets[bucket][key].max = Math.max(buckets[bucket][key].max, value);
    }
    return buckets;
}

export function destroyCharts() {
    if (_chart) {
        _chart.dispose();
        _chart = null;
    }
}

async function ensureECharts() {
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

    // Build series: always show averages, optionally add peaks as second series
    let series = [];

    // First pass: averages (always shown)
    const avgSeries = seriesKeys.map(key => {
        const [pid, name, windowType] = key.split('|');
        if (providerCounts[pid] === undefined) providerCounts[pid] = 0;
        const style = getSeriesStyle(pid, providerCounts[pid]++);

        return {
            name: `${pid.toUpperCase()}: ${name}`,
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

    // If showing peaks, add peak series with shaded area on top
    if (showPeaks) {
        const peakCounts = {};
        const peakSeries = seriesKeys.map(key => {
            const [pid, name, windowType] = key.split('|');
            if (peakCounts[pid] === undefined) peakCounts[pid] = 0;
            const style = getSeriesStyle(pid, peakCounts[pid]++);

            return {
                name: `${pid.toUpperCase()}: ${name} (Peak)`,
                type: 'line',
                smooth: true,
                symbol: 'circle',
                symbolSize: 4,
                showSymbol: bucketEpochs.length < 100,
                lineStyle: {
                    width: 1.5,
                    type: 'dashed',
                    dashOffset: 0,
                },
                itemStyle: { color: style.color },
                areaStyle: {
                    color: {
                        type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
                        colorStops: [
                            { offset: 0, color: style.color + '40' },
                            { offset: 1, color: style.color + '00' }
                        ]
                    }
                },
                data: bucketEpochs.map(ep => {
                    const b = buckets[ep]?.[key];
                    return b ? parseFloat(b.max.toFixed(2)) : null;
                }),
                connectNulls: true,
                emphasis: {
                    focus: 'series',
                    lineStyle: { width: 3 }
                },
                z: 1
            };
        });
        series = [...series, ...peakSeries];
    }

    try {
        const echarts = await ensureECharts();
        if (!_chart) {
            _chart = echarts.init(container);
        }

        const option = {
            backgroundColor: 'transparent',
            tooltip: {
                trigger: 'item',
                backgroundColor: 'rgba(24, 24, 27, 0.95)',
                borderColor: 'rgba(63, 63, 70, 0.5)',
                borderWidth: 1,
                padding: [10, 15],
                textStyle: { color: '#f4f4f5', fontFamily: 'JetBrains Mono', fontSize: 11 },
                formatter: (params) => {
                    const unit = metric === 'percent' ? '%' : (metric === 'cost' ? ' USD' : '');
                    return `
                        <div style="margin-bottom: 4px; color: #71717a; font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em;">${params.name}</div>
                        <div style="display: flex; align-items: center; gap: 8px;">
                            <span style="display: inline-block; width: 8px; height: 8px; border-radius: 50%; background-color: ${params.color}"></span>
                            <span style="font-weight: 700; color: #f4f4f5;">${params.seriesName}</span>
                            <span style="margin-left: 12px; font-family: 'JetBrains Mono'; color: #3b82f6;">${params.value}${unit}</span>
                        </div>
                    `;
                }
            },
            legend: {
                type: 'scroll',
                bottom: 10,
                textStyle: { color: '#71717a', fontSize: 10 },
                pageTextStyle: { color: '#71717a' },
                icon: 'roundRect'
            },
            grid: {
                top: 40,
                left: 60,
                right: 30,
                bottom: 110,
                containLabel: false
            },
            xAxis: {
                type: 'category',
                boundaryGap: false,
                data: xAxisData,
                axisLabel: { color: '#52525b', fontSize: 9, margin: 15 },
                axisLine: { lineStyle: { color: 'rgba(39, 39, 42, 0.5)' } },
                axisTick: { show: false }
            },
            yAxis: {
                type: 'value',
                name: yLabel,
                nameTextStyle: { color: '#52525b', fontSize: 9, align: 'right' },
                axisLabel: { color: '#52525b', fontSize: 9 },
                splitLine: { lineStyle: { color: 'rgba(39, 39, 42, 0.5)', type: 'dashed' } },
                axisLine: { show: false }
            },
            toolbox: {
                show: true,
                right: 20,
                top: 0,
                feature: {
                    myRange1h: { show: true, title: '1h', icon: 'path://M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10 10-4.5 10-10S17.5 2 12 2zm4.2 14.2L11 13V7h1.5v5.2l4.5 2.7-.8 1.3z', onclick: () => window.setHistoryDays(0.042) },
                    myRange6h: { show: true, title: '6h', icon: 'path://M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10 10-4.5 10-10S17.5 2 12 2zm1 10V7h-2v6h5v-2h-3z', onclick: () => window.setHistoryDays(0.25) },
                    myRange1d: { show: true, title: '1d', icon: 'path://M19 4h-1V2h-2v2H8V2H6v2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 16H5V10h14v10zm0-12H5V6h14v2z', onclick: () => window.setHistoryDays(1) },
                    myRange7d: { show: true, title: '7d', icon: 'path://M19 19H5V8h14v11zM19 3h-1V1h-2v2H8V1H6v2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-7 8h5v5h-5v-5z', onclick: () => window.setHistoryDays(7) },
                    myRange30d: { show: true, title: '30d', icon: 'path://M19 4h-1V2h-2v2H8V2H6v2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 16H5V10h14v10zm0-12H5V6h14v2z', onclick: () => window.setHistoryDays(30) },
                    myRange90d: { show: true, title: '90d', icon: 'path://M19 4h-1V2h-2v2H8V2H6v2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 16H5V10h14v10zm0-12H5V6h14v2z', onclick: () => window.setHistoryDays(90) }
                },
                iconStyle: {
                    borderColor: '#52525b',
                    borderWidth: 1
                },
                emphasis: {
                    iconStyle: {
                        borderColor: '#3b82f6'
                    }
                }
            },
            dataZoom: [
                {
                    type: 'inside',
                    start: 0,
                    end: 100
                },
                {
                    type: 'slider',
                    bottom: 45,
                    height: 20,
                    borderColor: 'transparent',
                    fillerColor: 'rgba(59, 130, 246, 0.1)',
                    handleIcon: 'path://M10.7,11.9v-1.3H9.3v1.3c-4.9,0.3-8.8,4.4-8.8,9.4c0,5,3.9,9.1,8.8,9.4v1.3h1.3v-1.3c4.9-0.3,8.8-4.4,8.8-9.4C19.5,16.3,15.6,12.2,10.7,11.9z M13.3,24.4H6.7V23h6.6V24.4z M13.3,19.6H6.7v-1.4h6.6V19.6z',
                    handleSize: '80%',
                    handleStyle: { color: '#3f3f46' },
                    textStyle: { color: 'transparent' },
                    dataBackground: {
                        lineStyle: { color: '#3b82f6', opacity: 0.2 },
                        areaStyle: { color: '#3b82f6', opacity: 0.1 }
                    }
                }
            ],
            series: series
        };
        
        _chart.setOption(option, true);  // true = notMerge: replace all series/axes cleanly

    } catch (err) {
        console.error('Failed to init ECharts:', err);
        emptyEl.textContent = 'Failed to load chart. Please refresh.';
        emptyEl?.classList.remove("hidden");
    }
}
