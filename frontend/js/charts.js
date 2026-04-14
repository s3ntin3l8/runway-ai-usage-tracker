// frontend/js/charts.js
// Chart.js wrapper for the History tab usage panel.
// Chart.js is lazy-loaded on first use of the History view.

let _chart = null;
let _chartJS = null;

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

function colorFor(providerId) {
    return PROVIDER_COLORS[providerId] || "#64748b";
}

/**
 * Pick bucket granularity for a given window. Mirrors backend _pick_bucket_seconds.
 * @param {number} days
 * @returns {number} bucket size in seconds
 */
export function pickBucketSeconds(days) {
    if (days >= 2) return 86400;  // 7d/30d/90d → daily
    if (days >= 0.5) return 3600; // 1d → hourly
    if (days >= 0.1) return 900;  // 6h → 15 min
    return 60;                     // 1h → 1 min
}

/**
 * Canonical bucket key for an ISO timestamp at a given granularity.
 * Returns the bucket-start epoch (number) for stable Map/Set equality.
 */
export function bucketKeyFor(isoTs, bucketSeconds) {
    const t = Math.floor(new Date(isoTs).getTime() / 1000);
    return t - (t % bucketSeconds);
}

/**
 * Human label for a bucket epoch, formatted at the granularity of the bucket.
 */
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

function extractLabelsAndProviders(snapshots, bucketSeconds) {
    const bucketEpochs = new Set();
    const providers = new Set();
    for (const s of snapshots) {
        bucketEpochs.add(bucketKeyFor(s.timestamp, bucketSeconds));
        providers.add(s.provider_id || 'unknown');
    }
    const sortedEpochs = Array.from(bucketEpochs).sort((a, b) => a - b);
    return {
        bucketEpochs: sortedEpochs,
        labels: sortedEpochs.map(e => formatBucketLabel(e, bucketSeconds)),
        providers: Array.from(providers),
    };
}

/**
 * Bucket snapshots by (bucket, provider) at the given granularity.
 * @returns {Object} { [bucketEpoch]: { provider_id: { sum, count } } }
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
            // percent: use direct percent value or derive ratio from quota-type units
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
        const pid = snap.provider_id || "unknown";
        if (!buckets[bucket]) buckets[bucket] = {};
        if (!buckets[bucket][pid]) buckets[bucket][pid] = { sum: 0, count: 0 };
        buckets[bucket][pid].sum += value;
        buckets[bucket][pid].count += 1;
    }
    return buckets;
}

export function destroyCharts() {
    if (_chart) { _chart.destroy(); _chart = null; }
}

async function ensureChartJS() {
    if (_chartJS) return _chartJS;
    if (window.Chart) {
        _chartJS = window.Chart;
        return _chartJS;
    }
    // Dynamically import and execute the script in global context
    const chartModule = await import('./lib/chart.min.js');
    // UMD attaches to 'this' (window in browser), not as default export
    // Re-import via script tag to get it onto window
    if (!window.Chart) {
        await new Promise((resolve, reject) => {
            const script = document.createElement('script');
            script.src = '/static/js/lib/chart.min.js';
            script.onload = () => resolve();
            script.onerror = () => reject(new Error('Failed to load Chart.js'));
            document.head.appendChild(script);
        });
    }
    _chartJS = window.Chart;
    return _chartJS;
}

/**
 * @param {Array} snapshots - history snapshot objects
 * @param {'percent'|'tokens'|'cost'} [metric='percent'] - which value to plot
 * @param {number} [days=7] - active history window; picks bucket granularity
 */
export async function updateCharts(snapshots, metric = 'percent', days = 7) {
    destroyCharts();

    const canvas = document.getElementById("chart-usage");
    const emptyEl = document.getElementById("chart-empty");
    if (!canvas) return;

    if (!snapshots || snapshots.length === 0) {
        emptyEl?.classList.remove("hidden");
        document.getElementById("chart-wrap")?.classList.add("hidden");
        return;
    }
    emptyEl?.classList.add("hidden");
    document.getElementById("chart-wrap")?.classList.remove("hidden");

    const bucketSeconds = pickBucketSeconds(days);
    const { bucketEpochs, labels, providers } = extractLabelsAndProviders(snapshots, bucketSeconds);
    const buckets = bucketByMetric(snapshots, metric, bucketSeconds);

    // Check if there's any data for this metric
    const hasData = bucketEpochs.some(ep => providers.some(p => buckets[ep]?.[p]));
    if (!hasData) {
        emptyEl?.classList.remove("hidden");
        document.getElementById("chart-wrap")?.classList.add("hidden");
        return;
    }

    const yLabel = metric === 'cost' ? 'Cost (USD)' : metric === 'tokens' ? 'Tokens' : '% Used';

    const datasets = providers.map(provider => {
        const color = colorFor(provider);
        return {
            label: provider.toUpperCase(),
            data: bucketEpochs.map(ep => {
                const b = buckets[ep]?.[provider];
                return b ? parseFloat((b.sum / b.count).toFixed(2)) : null;
            }),
            borderColor: color,
            backgroundColor: color + "15",
            borderWidth: 2,
            tension: 0.3,
            spanGaps: true,
            pointRadius: 2,
            pointHoverRadius: 5,
            fill: true,
        };
    });

    const options = {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 300 },
        plugins: {
            legend: {
                position: 'top', align: 'end',
                labels: { color: "#71717a", font: { size: 10, weight: 'bold' }, usePointStyle: true, boxWidth: 6 }
            },
            tooltip: {
                mode: "index", intersect: false,
                backgroundColor: 'rgba(24, 24, 27, 0.95)',
                titleColor: '#f4f4f5', bodyColor: '#a1a1aa',
                borderColor: 'rgba(63, 63, 70, 0.5)', borderWidth: 1,
                padding: 10, bodyFont: { family: 'JetBrains Mono' }
            },
        },
        scales: {
            x: { ticks: { color: "#52525b", font: { size: 9 }, maxTicksLimit: 7 }, grid: { display: false } },
            y: {
                beginAtZero: true,
                ticks: { color: "#52525b", font: { size: 9 }, maxTicksLimit: 5 },
                grid: { color: "rgba(39, 39, 42, 0.5)" },
                title: { display: true, text: yLabel, color: '#52525b', font: { size: 9 } },
            },
        },
    };

    try {
        const Chart = await ensureChartJS();
        _chart = new Chart(canvas.getContext("2d"), {
            type: "line",
            data: { labels, datasets },
            options,
        });
    } catch (err) {
        console.error('Failed to load Chart.js:', err);
        emptyEl.textContent = 'Failed to load chart. Please refresh.';
        emptyEl?.classList.remove("hidden");
    }
}

export function setChartView(view) {
    // Legacy support for app.js calls
}
