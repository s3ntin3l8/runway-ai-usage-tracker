// frontend/js/charts.js
// Chart.js wrapper for the History tab usage panel.
// Depends on Chart.js being loaded globally (CDN in index.html).

let _chart = null;

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

function extractLabelsAndProviders(snapshots) {
    const days = new Set();
    const providers = new Set();
    for (const s of snapshots) {
        days.add(s.timestamp.slice(0, 10));
        if (s.provider_id) providers.add(s.provider_id);
    }
    return { labels: Array.from(days).sort(), providers: Array.from(providers) };
}

/**
 * Bucket snapshots by day and provider, extracting the correct metric value.
 * @param {Array} snapshots
 * @param {'percent'|'tokens'|'cost'} metric
 * @returns {Object} { "YYYY-MM-DD": { provider_id: { sum, count } } }
 */
function bucketByDayMetric(snapshots, metric) {
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
            // percent: use direct percent value or derive from used/limit
            if (snap.unit_type === 'percent' && snap.used_value != null) {
                value = snap.used_value;
            } else if (snap.used_value != null && snap.limit_value > 0) {
                value = (snap.used_value / snap.limit_value) * 100;
            } else {
                continue;
            }
        }
        const day = snap.timestamp.slice(0, 10);
        const pid = snap.provider_id || "unknown";
        if (!buckets[day]) buckets[day] = {};
        if (!buckets[day][pid]) buckets[day][pid] = { sum: 0, count: 0 };
        buckets[day][pid].sum += value;
        buckets[day][pid].count += 1;
    }
    return buckets;
}

export function destroyCharts() {
    if (_chart) { _chart.destroy(); _chart = null; }
}

/**
 * @param {Array} snapshots - history snapshot objects
 * @param {'percent'|'tokens'|'cost'} [metric='percent'] - which value to plot
 */
export function updateCharts(snapshots, metric = 'percent') {
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

    const { labels, providers } = extractLabelsAndProviders(snapshots);
    const buckets = bucketByDayMetric(snapshots, metric);

    // Check if there's any data for this metric
    const hasData = labels.some(day => providers.some(p => buckets[day]?.[p]));
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
            data: labels.map(day => {
                const b = buckets[day]?.[provider];
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

    _chart = new Chart(canvas.getContext("2d"), {
        type: "line",
        data: { labels, datasets },
        options,
    });
}

export function setChartView(view) {
    // Legacy support for app.js calls
}
