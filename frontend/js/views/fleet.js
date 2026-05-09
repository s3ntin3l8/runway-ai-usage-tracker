import { fetchFleet, patchSidecar, deleteSidecarAPI, setSidecarEnabledAPI } from '../api.js';
import { buildFleetView } from '../components.js';

function escapeHTML(str) {
    if (!str) return '';
    const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
    return str.replace(/[&<>"']/g, m => map[m]);
}

export async function loadFleetView() {
    const container = document.getElementById('fleet-content');
    if (!container) return;
    container.innerHTML = '<p class="text-zinc-500 animate-pulse">Loading fleet...</p>';
    try {
        const data = await fetchFleet();
        container.innerHTML = buildFleetView(data.sidecars);
    } catch (err) {
        container.innerHTML = `<p class="text-red-400">Failed to load fleet: ${escapeHTML(err.message)}</p>`;
    }
}

export async function editSidecarName(sidecarId) {
    const newName = prompt('Enter a custom name for this sidecar:', '');
    if (newName === null) return;
    try {
        await patchSidecar(sidecarId, { custom_name: newName.trim() || null });
        await loadFleetView();
    } catch (err) {
        alert('Failed to rename: ' + err.message);
    }
}

export async function addSidecarTag(sidecarId) {
    const tag = prompt('Enter a tag for this sidecar:');
    if (!tag || !tag.trim()) return;
    try {
        const fleet = await fetchFleet();
        const sidecar = fleet.sidecars.find(s => s.sidecar_id === sidecarId);
        const tags = [...(sidecar?.tags || []), tag.trim()];
        await patchSidecar(sidecarId, { tags });
        await loadFleetView();
    } catch (err) {
        alert('Failed to add tag: ' + err.message);
    }
}

export async function deleteSidecar(sidecarId) {
    if (!confirm(`Remove sidecar "${sidecarId}" from the registry?`)) return;
    try {
        await deleteSidecarAPI(sidecarId);
        await loadFleetView();
    } catch (err) {
        alert('Failed to delete: ' + err.message);
    }
}

export async function toggleSidecarEnabled(sidecarId, currentlyEnabled) {
    // currentlyEnabled is the state BEFORE the click — clicking flips it.
    const willEnable = !currentlyEnabled;
    const verb = willEnable ? 'resume' : 'pause';
    const desc = willEnable
        ? `Resume collection on "${sidecarId}"?`
        : `Stop collection on "${sidecarId}"?\n\nThe sidecar will keep checking in but won't collect from any providers until you resume it.`;
    if (!confirm(desc)) return;

    const card = document.querySelector(`[data-sidecar="${CSS.escape(sidecarId)}"]`);
    const btn = card?.querySelector('button[data-sidecar-toggle]');
    const originalHTML = btn?.innerHTML ?? '';

    const SVG_SPIN = '<svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="animation:spin .7s linear infinite"><circle cx="12" cy="12" r="10" stroke-opacity=".25"/><path d="M12 2a10 10 0 0 1 10 10"/></svg>';
    const SVG_CHECK = '<svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>';
    const SVG_ERR   = '<svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';

    function setBtnState(svg, cls, disabled) {
        if (!btn) return;
        btn.innerHTML = svg;
        btn.className = btn.className.replace(/\btext-good-c\b|\btext-crit-c\b/g, '').trim();
        if (cls) btn.classList.add(cls);
        btn.disabled = !!disabled;
    }

    setBtnState(SVG_SPIN, '', true);

    try {
        await setSidecarEnabledAPI(sidecarId, willEnable);
        setBtnState(SVG_CHECK, 'text-good-c', true);
        // Reload the fleet to render the new steady-state icon (pause vs play)
        setTimeout(() => loadFleetView(), 1200);
    } catch (err) {
        setBtnState(SVG_ERR, 'text-crit-c', false);
        setTimeout(() => {
            if (btn) btn.innerHTML = originalHTML;
            setBtnState(originalHTML, '', false);
        }, 2500);
        alert(`Failed to ${verb} sidecar: ` + err.message);
    }
}

export function initFleetView() {
    // Fleet view uses inline onclick handlers in HTML
}