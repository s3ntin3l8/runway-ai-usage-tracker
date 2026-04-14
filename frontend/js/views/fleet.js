import { fetchFleet, patchSidecar, deleteSidecarAPI } from '../api.js';
import { buildFleetView } from '../components.js';

function escapeHTML(str) {
    if (!str) return '';
    const map = { '&': '&amp;', '<': '&gt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
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

export function initFleetView() {
    // Fleet view uses inline onclick handlers in HTML
}