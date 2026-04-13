// frontend/js/charts.js
// Chart.js wrapper for the History tab token volume panel.
// Depends on Chart.js being loaded globally (CDN in index.html).

let _barChart = null;
let _lineChart = null;

const PROVIDER_COLORS = {
    anthropic: "#f59e0b",
    openai: "#10b981",
    google: "#3b82f6",
    github: "#8b5cf6",
    ollama: "#06b6d4",
    openrouter: "#ec4899",
    minimax: "#14b8a6",
};

function colorFor(providerId) {
    return PROVIDER_COLORS[providerId] || "#6b7280";
}

function bucketByDay(snapshots) {
    // Returns { "YYYY-MM-DD": { provider_id: { sum, count } } }
    const buckets = {};
    for (const snap of snapshots) {
        if (snap.used_value == null) continue;
        const day = snap.timestamp.slice(0, 10);
        if (!buckets[day]) buckets[day] = {};
        const pid = snap.provider_id || "unknown";
        if (!buckets[day][pid]) buckets[day][pid] = { sum: 0, count: 0 };
        buckets[day][pid].sum += snap.used_value;
        buckets[day][pid].count += 1;
    }
    return buckets;
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

function latestLimitPerProvider(snapshots) {
    const limits = {};
    for (const s of snapshots) {
        if (s.provider_id && s.limit_value != null) {
            limits[s.provider_id] = s.limit_value;
        }
    }
    return limits;
}

export function destroyCharts() {
    if (_barChart) { _barChart.destroy(); _barChart = null; }
    if (_lineChart) { _lineChart.destroy(); _lineChart = null; }
}

export function updateCharts(snapshots, activeView = "bar") {
    destroyCharts();

    const barCanvas = document.getElementById("chart-bar");
    const lineCanvas = document.getElementById("chart-line");
    const emptyEl = document.getElementById("chart-empty");
    if (!barCanvas || !lineCanvas) return;

    if (!snapshots || snapshots.length === 0) {
        emptyEl?.classList.remove("hidden");
        return;
    }
    emptyEl?.classList.add("hidden");

    const { labels, providers } = extractLabelsAndProviders(snapshots);
    const buckets = bucketByDay(snapshots);
    const limits = latestLimitPerProvider(snapshots);

    // --- Bar chart (stacked by provider) ---
    const barDatasets = providers.map(provider => ({
        label: provider,
        data: labels.map(day => {
            const b = buckets[day]?.[provider];
            return b ? Math.round(b.sum / b.count) : 0;
        }),
        backgroundColor: colorFor(provider),
        stack: "combined",
        borderRadius: 2,
    }));

    const chartDefaults = {
        responsive: true,
        animation: false,
        plugins: {
            legend: { labels: { color: "#a1a1aa", font: { size: 11 } } },
            tooltip: { mode: "index", intersect: false },
        },
        scales: {
            x: {
                stacked: true,
                ticks: { color: "#71717a", maxTicksLimit: 10 },
                grid: { color: "#27272a" },
            },
            y: {
                stacked: true,
                ticks: { color: "#71717a" },
                grid: { color: "#27272a" },
            },
        },
    };

    _barChart = new Chart(barCanvas.getContext("2d"), {
        type: "bar",
        data: { labels, datasets: barDatasets },
        options: { ...chartDefaults },
    });

    // --- Line chart (per provider + limit reference line) ---
    const lineDatasets = providers.flatMap(provider => {
        const color = colorFor(provider);
        const datasets = [{
            label: provider,
            data: labels.map(day => {
                const b = buckets[day]?.[provider];
                return b ? Math.round(b.sum / b.count) : null;
            }),
            borderColor: color,
            backgroundColor: color + "22",
            tension: 0.3,
            spanGaps: true,
            pointRadius: 3,
        }];
        if (limits[provider]) {
            datasets.push({
                label: `${provider} limit`,
                data: labels.map(() => limits[provider]),
                borderColor: color,
                borderDash: [6, 3],
                pointRadius: 0,
                tension: 0,
                fill: false,
            });
        }
        return datasets;
    });

    const lineOptions = {
        responsive: true,
        animation: false,
        plugins: {
            legend: { labels: { color: "#a1a1aa", font: { size: 11 } } },
        },
        scales: {
            x: { ticks: { color: "#71717a", maxTicksLimit: 10 }, grid: { color: "#27272a" } },
            y: { ticks: { color: "#71717a" }, grid: { color: "#27272a" } },
        },
    };

    _lineChart = new Chart(lineCanvas.getContext("2d"), {
        type: "line",
        data: { labels, datasets: lineDatasets },
        options: lineOptions,
    });

    // Show the active view
    setChartViewVisibility(activeView);
}

function setChartViewVisibility(view) {
    const barWrap = document.getElementById("chart-bar-wrap");
    const lineWrap = document.getElementById("chart-line-wrap");
    const barBtn = document.getElementById("chart-view-bar");
    const lineBtn = document.getElementById("chart-view-line");
    if (view === "bar") {
        barWrap?.classList.remove("hidden");
        lineWrap?.classList.add("hidden");
        barBtn?.classList.add("active");
        lineBtn?.classList.remove("active");
    } else {
        barWrap?.classList.add("hidden");
        lineWrap?.classList.remove("hidden");
        barBtn?.classList.remove("active");
        lineBtn?.classList.add("active");
    }
}

export function setChartView(view) {
    setChartViewVisibility(view);
}
