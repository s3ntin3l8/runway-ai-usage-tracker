// Lazy-loads Sortable.js from a CDN on first use.
// Mirrors the ensureChartJS pattern in charts.js.

let _sortable = null;
let _loading = null;

const CDN = 'https://cdn.jsdelivr.net/npm/sortablejs@1.15.2/Sortable.min.js';

export async function ensureSortable() {
    if (_sortable) return _sortable;
    if (window.Sortable) {
        _sortable = window.Sortable;
        return _sortable;
    }
    if (!_loading) {
        _loading = new Promise((resolve, reject) => {
            const script = document.createElement('script');
            script.src = CDN;
            script.onload = () => resolve(window.Sortable);
            script.onerror = () => reject(new Error('Failed to load Sortable.js'));
            document.head.appendChild(script);
        });
    }
    _sortable = await _loading;
    return _sortable;
}
