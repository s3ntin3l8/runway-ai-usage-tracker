import { fetchFleet, patchSidecar, deleteSidecarAPI, triggerSidecarCollectAPI } from '../api.js';
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

export async function triggerSidecarCollect(sidecarId) {
    try {
        await triggerSidecarCollectAPI(sidecarId);
        // Brief visual feedback — no full reload needed
        const card = document.querySelector(`[data-sidecar="${CSS.escape(sidecarId)}"]`);
        if (card) {
            const btn = card.querySelector('button[title^="Run Now"]');
            if (btn) {
                const orig = btn.innerHTML;
                btn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>';
                btn.classList.add('text-green-400');
                setTimeout(() => { btn.innerHTML = orig; btn.classList.remove('text-green-400'); }, 2000);
            }
        }
    } catch (err) {
        alert('Failed to trigger collection: ' + err.message);
    }
}

export function initFleetView() {
    // Fleet view uses inline onclick handlers in HTML
}