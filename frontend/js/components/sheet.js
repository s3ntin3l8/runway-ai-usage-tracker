/**
 * Bottom-sheet utility (mobile, ≤640px).
 *
 * Two consumers with different ownership models:
 *
 *  1. Filter sheets (dashboard + history) — the sheet OWNS its lifecycle:
 *     `openSheet(el)` / `closeSheet(el)` toggle the `.open` class on a
 *     `.sheet-bg` wrapper; backdrop click, Escape, and drag-to-dismiss all
 *     route through `closeSheet`.
 *
 *  2. The provider detail modal — presentation only. The modal keeps its own
 *     open/close functions (which do required teardown: chart disposal, body
 *     scroll restore, cache clear), so drag-to-dismiss attaches via
 *     `attachSheetDrag(panel, { onDismiss: closeProviderModal })` and never
 *     hides the element itself.
 *
 * Drag-to-dismiss grabs only on elements matching `gripSelector` (grip bar /
 * header) so the sheet body keeps native scrolling.
 */

const _DISMISS_PX = 150;       // absolute drag distance that dismisses…
const _DISMISS_FRACTION = 0.3; // …or this fraction of the panel height

/**
 * Attach drag-to-dismiss to a sheet panel.
 *
 * @param {HTMLElement} panel - the sliding panel (translated during drag)
 * @param {{ onDismiss: () => void, gripSelector?: string }} opts
 * @returns {() => void} detach function
 */
export function attachSheetDrag(panel, { onDismiss, gripSelector = '.sheet-grip, .sheet-head' }) {
    let startY = null;
    let delta = 0;

    const down = (e) => {
        if (!e.target.closest(gripSelector)) return;
        if (e.target.closest('button, a, input, select')) return;
        startY = e.touches ? e.touches[0].clientY : e.clientY;
        delta = 0;
        panel.classList.add('dragging');
    };
    const move = (e) => {
        if (startY === null) return;
        const y = e.touches ? e.touches[0].clientY : e.clientY;
        delta = Math.max(0, y - startY);
        panel.style.transform = `translateY(${delta}px)`;
    };
    const up = () => {
        if (startY === null) return;
        panel.classList.remove('dragging');
        const threshold = Math.min(_DISMISS_PX, panel.offsetHeight * _DISMISS_FRACTION);
        panel.style.transform = '';
        if (delta > threshold) onDismiss();
        startY = null;
        delta = 0;
    };

    panel.addEventListener('pointerdown', down);
    panel.addEventListener('touchstart', down, { passive: true });
    window.addEventListener('pointermove', move);
    window.addEventListener('touchmove', move, { passive: true });
    window.addEventListener('pointerup', up);
    window.addEventListener('touchend', up);

    return () => {
        panel.removeEventListener('pointerdown', down);
        panel.removeEventListener('touchstart', down);
        window.removeEventListener('pointermove', move);
        window.removeEventListener('touchmove', move);
        window.removeEventListener('pointerup', up);
        window.removeEventListener('touchend', up);
    };
}

/**
 * Open a self-owned sheet (`.sheet-bg` wrapper containing a `.sheet` panel).
 * Wires backdrop click, Escape, and drag-to-dismiss on first open.
 */
export function openSheet(bg) {
    if (!bg) return;
    if (!bg._sheetWired) {
        bg._sheetWired = true;
        bg.addEventListener('click', (e) => { if (e.target === bg) closeSheet(bg); });
        bg._escHandler = (e) => {
            if (e.key === 'Escape' && bg.classList.contains('open')) closeSheet(bg);
        };
        document.addEventListener('keydown', bg._escHandler);
        const panel = bg.querySelector('.sheet');
        if (panel) attachSheetDrag(panel, { onDismiss: () => closeSheet(bg) });
    }
    // Two-frame open so the off-screen "from" state paints before the
    // transition class lands (otherwise the slide-up never animates).
    bg.removeAttribute('hidden');
    requestAnimationFrame(() => requestAnimationFrame(() => bg.classList.add('open')));
}

/** Close a self-owned sheet; re-hides after the slide-down transition. */
export function closeSheet(bg) {
    if (!bg) return;
    bg.classList.remove('open');
    setTimeout(() => { if (!bg.classList.contains('open')) bg.setAttribute('hidden', ''); }, 360);
}
