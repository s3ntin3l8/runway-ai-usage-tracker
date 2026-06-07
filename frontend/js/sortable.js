// Lazy-loads the vendored Sortable.js on first use.
// Mirrors the ensureECharts pattern in charts.js (CSP is script-src 'self',
// so the lib must be served from our own origin — no CDN).

let _sortable = null;
let _loading = null;

const SRC = '/static/js/lib/Sortable.min.js';

export async function ensureSortable() {
    if (_sortable) return _sortable;
    if (window.Sortable) {
        _sortable = window.Sortable;
        return _sortable;
    }
    if (!_loading) {
        _loading = new Promise((resolve, reject) => {
            const script = document.createElement('script');
            script.src = SRC;
            script.onload = () => resolve(window.Sortable);
            script.onerror = () => reject(new Error('Failed to load Sortable.js'));
            document.head.appendChild(script);
        });
    }
    _sortable = await _loading;
    return _sortable;
}
