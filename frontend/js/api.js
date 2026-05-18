/**
 * Centralized fetch wrapper that injects authentication headers
 */
async function fetchWithAuth(url, options = {}) {
    const adminKey = localStorage.getItem('runway_admin_key');
    const headers = options.headers || {};
    
    if (adminKey) {
        headers['X-Admin-Key'] = adminKey;
    }
    
    return fetch(url, { ...options, headers });
}

/**
 * Fetch the Fleet HUD aggregation: one entry per (provider_id, account_id)
 * with critical_gauge / secondary_limits / sidecar_contributions.
 * @returns {Promise<{fleet: Array<FleetEntry>, generated_at: string}>}
 */
export async function fetchUsageFleet() {
    const resp = await fetchWithAuth('/api/v1/usage/fleet');
    if (!resp.ok) throw new Error(`Failed to fetch fleet view: HTTP ${resp.status}`);
    return await resp.json();
}

/**
 * Fetch cumulative usage rolled up across sidecars per (provider_id, account_id).
 * @returns {Promise<{cumulative: Array<CumulativeEntry>, generated_at: string}>}
 */
export async function fetchCumulative() {
    const resp = await fetchWithAuth('/api/v1/usage/cumulative');
    if (!resp.ok) throw new Error(`Failed to fetch cumulative usage: HTTP ${resp.status}`);
    return await resp.json();
}

export async function fetchHeatmap(params = {}) {
    const qs = new URLSearchParams(params).toString();
    const resp = await fetchWithAuth(`/api/v1/usage/heatmap${qs ? '?' + qs : ''}`);
    if (!resp.ok) throw new Error(`Failed to fetch heatmap: HTTP ${resp.status}`);
    return await resp.json();
}

export async function fetchSessions(params = {}) {
    const qs = new URLSearchParams(params).toString();
    const resp = await fetchWithAuth(`/api/v1/usage/sessions${qs ? '?' + qs : ''}`);
    if (!resp.ok) throw new Error(`Failed to fetch sessions: HTTP ${resp.status}`);
    return await resp.json();
}

/**
 * Fetch all limits from the backend
 * @returns {Promise<{limits: Array<LimitCard>}>} Limits response
 * @throws {Error} Network, HTTP, or parsing errors with descriptive messages
 */
export async function fetchLimits() {
    try {
        const resp = await fetchWithAuth('/api/v1/usage/limits');
        
        if (!resp.ok) {
            // Provide specific error messages for different HTTP status codes
            const errorMessages = {
                404: 'API endpoint not found',
                500: 'Server error - please try again',
                503: 'Server temporarily unavailable',
            };
            const message = errorMessages[resp.status] || `HTTP ${resp.status} error`;
            throw new Error(message);
        }
        
        // Check if response is valid JSON
        const contentType = resp.headers.get('content-type');
        if (!contentType || !contentType.includes('application/json')) {
            throw new Error('Invalid response format from server');
        }
        
        return await resp.json();
    } catch (err) {
        // Distinguish between different error types
        if (err instanceof TypeError) {
            // Network error (no internet, CORS issue, etc.)
            throw new Error('Network error - unable to reach server');
        } else if (err instanceof SyntaxError) {
            // JSON parse error
            throw new Error('Invalid data format received from server');
        }
        // Re-throw other errors
        throw err;
    }
}

/**
 * GitHub OAuth Functions
 */

export async function initGitHubOAuth() {
    const resp = await fetchWithAuth('/api/v1/auth/github/init');
    if (!resp.ok) throw new Error('Failed to initiate GitHub login');
    return await resp.json();
}

export async function pollGitHubOAuth(deviceCode) {
    const resp = await fetchWithAuth('/api/v1/auth/github/poll', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_code: deviceCode })
    });
    if (!resp.ok) throw new Error('Polling failed');
    return await resp.json();
}

export async function getGitHubOAuthStatus() {
    const resp = await fetchWithAuth('/api/v1/auth/github/status');
    if (!resp.ok) return { authenticated: false };
    return await resp.json();
}

export async function logoutGitHub() {
    const resp = await fetchWithAuth('/api/v1/auth/github/logout', { method: 'POST' });
    if (!resp.ok) throw new Error('Logout failed');
    return await resp.json();
}

/**
 * History, Settings & Status
 */

export async function collectProvider(providerId, accountId) {
    const params = accountId ? `?account_id=${encodeURIComponent(accountId)}` : '';
    const resp = await fetchWithAuth(`/api/v1/usage/collect/${encodeURIComponent(providerId)}${params}`, { method: 'POST' });
    if (!resp.ok) throw new Error(`Collect failed: HTTP ${resp.status}`);
    return await resp.json();
}

export async function fetchHistory(params = {}) {
    const query = new URLSearchParams(params).toString();
    const resp = await fetchWithAuth(`/api/v1/usage/history?${query}`);
    if (!resp.ok) throw new Error('Failed to fetch history');
    return await resp.json();
}

export async function fetchHistoryRaw(params = {}) {
    const query = new URLSearchParams(params).toString();
    const resp = await fetchWithAuth(`/api/v1/usage/history/raw?${query}`);
    if (!resp.ok) throw new Error('Failed to fetch raw history');
    return await resp.json();
}

export async function fetchHistoryChart(params = {}) {
    const query = new URLSearchParams(params).toString();
    const resp = await fetchWithAuth(`/api/v1/usage/history/chart?${query}`);
    if (!resp.ok) throw new Error(`Failed to fetch chart history: HTTP ${resp.status}`);
    return await resp.json();
}

export async function fetchHistoryWindowDetail(params = {}) {
    const query = new URLSearchParams(params).toString();
    const resp = await fetchWithAuth(`/api/v1/usage/history/window-detail?${query}`);
    if (!resp.ok) throw new Error(`Failed to fetch window history: HTTP ${resp.status}`);
    return await resp.json();
}

export async function fetchHistoryDeltas(params = {}) {
    const query = new URLSearchParams(params).toString();
    const resp = await fetchWithAuth(`/api/v1/usage/history/deltas?${query}`);
    if (!resp.ok) throw new Error('Failed to fetch history deltas');
    return await resp.json();
}

export async function fetchForecast(params = {}) {
    const query = new URLSearchParams(params).toString();
    const resp = await fetchWithAuth(`/api/v1/usage/forecast?${query}`);
    if (!resp.ok) throw new Error(`Failed to fetch forecast: HTTP ${resp.status}`);
    return await resp.json();
}

export async function fetchSettings() {
    const resp = await fetchWithAuth('/api/v1/system/settings');
    if (!resp.ok) throw new Error('Failed to fetch settings');
    return await resp.json();
}

export async function fetchStatus() {
    const resp = await fetchWithAuth('/api/v1/system/status');
    if (!resp.ok) throw new Error('Failed to fetch collector status');
    return await resp.json();
}

/**
 * Fleet / Sidecar Registry
 */

export async function fetchFleet() {
    const resp = await fetchWithAuth('/api/v1/fleet/sidecars');
    if (!resp.ok) throw new Error('Failed to fetch fleet');
    return await resp.json();
}

export async function patchSidecar(sidecarId, body) {
    const resp = await fetchWithAuth(`/api/v1/fleet/sidecars/${encodeURIComponent(sidecarId)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error(`Failed to update sidecar: HTTP ${resp.status}`);
    return await resp.json();
}

export async function deleteSidecarAPI(sidecarId) {
    const resp = await fetchWithAuth(`/api/v1/fleet/sidecars/${encodeURIComponent(sidecarId)}`, {
        method: 'DELETE',
    });
    if (!resp.ok) throw new Error(`Failed to delete sidecar: HTTP ${resp.status}`);
    return await resp.json();
}

export async function setSidecarEnabledAPI(sidecarId, enabled) {
    const action = enabled ? 'resume' : 'pause';
    const resp = await fetchWithAuth(
        `/api/v1/fleet/sidecars/${encodeURIComponent(sidecarId)}/${action}`,
        { method: 'POST' },
    );
    if (!resp.ok) throw new Error(`Failed to ${action} sidecar: HTTP ${resp.status}`);
    return await resp.json();
}

/**
 * Token Health
 */

export async function forceCollect() {
    const resp = await fetchWithAuth('/api/v1/system/force-collect', { method: 'POST' });
    if (!resp.ok) throw new Error(`Force collect failed: HTTP ${resp.status}`);
    return await resp.json();
}

export async function postWake() {
    // Silently wake the poller (resumes normal interval if sleeping)
    try {
        await fetchWithAuth('/api/v1/system/wake', { method: 'POST' });
    } catch (e) {
        // Silently fail as this is a background optimization
        console.debug('Background wake failed:', e);
    }
}

export async function fetchTokenHealth() {
    const resp = await fetchWithAuth('/api/v1/system/token-health');
    if (!resp.ok) throw new Error('Failed to fetch token health');
    return await resp.json();
}

export async function postTokenRefresh(provider, accountId) {
    const resp = await fetchWithAuth(
        `/api/v1/system/token-health/refresh/${encodeURIComponent(provider)}/${encodeURIComponent(accountId)}`,
        { method: 'POST' }
    );
    if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.detail || `HTTP ${resp.status}`);
    }
    return await resp.json();
}

/**
 * Provider Configuration
 */

export async function fetchProviderConfigs() {
    const resp = await fetchWithAuth('/api/v1/system/provider-configs');
    if (!resp.ok) throw new Error(`Failed to fetch provider configs: HTTP ${resp.status}`);
    return await resp.json();
}

export async function fetchAppConfig() {
    const resp = await fetchWithAuth('/api/v1/system/app-config');
    if (!resp.ok) throw new Error(`Failed to fetch app config: HTTP ${resp.status}`);
    return await resp.json();
}

export async function putAppConfig(body) {
    const resp = await fetchWithAuth('/api/v1/system/app-config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.detail || `HTTP ${resp.status}`);
    }
    return await resp.json();
}

export async function putProviderConfig(providerId, body) {
    const resp = await fetchWithAuth(`/api/v1/system/provider-config/${encodeURIComponent(providerId)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.detail || `HTTP ${resp.status}`);
    }
    return await resp.json();
}

/**
 * Dashboard Layout
 */

export async function getDashboardLayout() {
    const resp = await fetchWithAuth('/api/v1/system/dashboard-layout');
    if (!resp.ok) throw new Error(`Failed to fetch dashboard layout: HTTP ${resp.status}`);
    return await resp.json();
}

export async function putDashboardLayout(layout) {
    const resp = await fetchWithAuth('/api/v1/system/dashboard-layout', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(layout),
    });
    if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.detail || `HTTP ${resp.status}`);
    }
    return await resp.json();
}
