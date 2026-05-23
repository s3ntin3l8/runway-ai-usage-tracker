/**
 * Chart axis scale utilities — no browser dependencies, importable in Node tests.
 */

/** Round v up to the nearest "nice" number (1/2/2.5/5/10 × 10^n). */
export function niceMax(v) {
    if (v <= 0) return 1;
    const mag = Math.pow(10, Math.floor(Math.log10(v)));
    const norm = v / mag;
    const nice = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10;
    return nice * mag;
}

/** Format a tick label with 1 decimal on K/M only when needed (e.g. 2500 → "2.5K"). */
export function fmtTick(v) {
    if (v >= 1e6) return (v / 1e6).toFixed(v % 1e6 === 0 ? 0 : 1) + 'M';
    if (v >= 1e3) return (v / 1e3).toFixed(v % 1e3 === 0 ? 0 : 1) + 'K';
    return Math.round(v).toString();
}
