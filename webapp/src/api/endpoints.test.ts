import {
  checkForUpdates,
  fetchAppConfig,
  fetchFleetUsage,
  fetchLimits,
  fetchPendingUsageEvents,
  fetchPendingUsageSessions,
  fetchProviderConfigs,
  patchCredentialSources,
  fetchSidecars,
  fetchStatus,
  fetchTokenHealth,
  fetchWebhooks,
  deleteProviderConfig,
  forceCollect,
  getDashboardLayout,
  getGitHubOAuthStatus,
  assignPendingUsageEvents,
  assignPendingUsageEventsBatch,
  initGitHubOAuth,
  logoutGitHub,
  postWake,
  updateWebhook,
} from './endpoints';

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
}

function mockFetch() {
  return fetch as unknown as ReturnType<typeof vi.fn>;
}

// Read the (path, init) the wrapper passed to fetch on its first/only call.
function lastCall(): [string, RequestInit] {
  return mockFetch().mock.calls[0] as [string, RequestInit];
}

describe('endpoints', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.stubGlobal('fetch', vi.fn());
  });
  afterEach(() => vi.unstubAllGlobals());

  // --- GET wrappers (default method, no body) ---

  it('fetchLimits hits the limits path and returns the payload', async () => {
    const payload = { limits: [{ provider_id: 'claude' }] };
    mockFetch().mockResolvedValue(jsonResponse(payload));
    const data = await fetchLimits();
    expect(data).toEqual(payload);
    const [path, init] = lastCall();
    expect(path).toBe('/api/v1/usage/limits');
    expect(init.method).toBeUndefined();
  });

  it('fetchFleetUsage hits the fleet path', async () => {
    const payload = { cards: [], window_aggregations: { longest: {} } };
    mockFetch().mockResolvedValue(jsonResponse(payload));
    const data = await fetchFleetUsage();
    expect(data).toEqual(payload);
    expect(lastCall()[0]).toBe('/api/v1/usage/fleet');
  });

  it('fetchPendingUsageEvents requests a page of unassigned usage', async () => {
    const payload = { items: [], total: 0, offset: 100, limit: 100 };
    mockFetch().mockResolvedValue(jsonResponse(payload));
    await expect(fetchPendingUsageEvents(100)).resolves.toEqual(payload);
    expect(lastCall()[0]).toBe('/api/v1/fleet/events/pending?offset=100&limit=100');
  });

  it('fetchPendingUsageSessions requests grouped pending usage', async () => {
    const payload = {
      items: [], total_events: 0, total_groups: 0, sidecars: [], providers: [], offset: 100, limit: 100,
    };
    mockFetch().mockResolvedValue(jsonResponse(payload));
    await expect(fetchPendingUsageSessions(100)).resolves.toEqual(payload);
    expect(lastCall()[0]).toBe('/api/v1/fleet/events/pending/sessions?offset=100&limit=100');
  });

  it('fetchPendingUsageSessions sends filters and a requested page size', async () => {
    mockFetch().mockResolvedValue(jsonResponse({ items: [], sidecars: [], providers: [] }));
    await fetchPendingUsageSessions(0, { sidecar_id: 'host 1', provider_id: 'xai', search: 'grok' }, 500);
    expect(lastCall()[0]).toBe(
      '/api/v1/fleet/events/pending/sessions?offset=0&limit=500&sidecar_id=host+1&provider_id=xai&search=grok',
    );
  });

  it('fetchSidecars hits the sidecars path', async () => {
    const payload = { sidecars: [{ id: 'host-1' }] };
    mockFetch().mockResolvedValue(jsonResponse(payload));
    const data = await fetchSidecars();
    expect(data).toEqual(payload);
    expect(lastCall()[0]).toBe('/api/v1/fleet/sidecars');
  });

  it('fetchStatus hits the system status path', async () => {
    const payload = { running: true };
    mockFetch().mockResolvedValue(jsonResponse(payload));
    const data = await fetchStatus();
    expect(data).toEqual(payload);
    expect(lastCall()[0]).toBe('/api/v1/system/status');
  });

  it('fetchAppConfig hits the app-config path', async () => {
    const payload = { browser: 'chrome' };
    mockFetch().mockResolvedValue(jsonResponse(payload));
    const data = await fetchAppConfig();
    expect(data).toEqual(payload);
    expect(lastCall()[0]).toBe('/api/v1/system/app-config');
  });

  it('fetchProviderConfigs hits the provider-configs path', async () => {
    const payload = { providers: [{ provider_id: 'claude', enabled: true }] };
    mockFetch().mockResolvedValue(jsonResponse(payload));
    const data = await fetchProviderConfigs();
    expect(data).toEqual(payload);
    expect(lastCall()[0]).toBe('/api/v1/system/provider-configs');
  });

  it('patchCredentialSources sends source order, toggles, and machine scope', async () => {
    mockFetch().mockImplementation(() => Promise.resolve(jsonResponse({ status: 'ok' })));
    const source = { source_id: 'sidecar:browser', enabled: true, priority: 0 };

    await expect(patchCredentialSources('open/router', 'alice@example.com', [source], true)).resolves.toEqual({
      status: 'ok',
    });
    let [path, init] = lastCall();
    expect(path).toBe(
      '/api/v1/system/provider-config/open%2Frouter/alice%40example.com/credential-sources',
    );
    expect(init.method).toBe('PATCH');
    expect(JSON.parse(String(init.body))).toEqual({ sources: [source], all_machines: true });

    await patchCredentialSources('openrouter', 'default', [source]);
    [path, init] = mockFetch().mock.calls.at(-1) as [string, RequestInit];
    expect(path).toContain('/openrouter/default/credential-sources');
    expect(JSON.parse(String(init.body))).toEqual({ sources: [source], all_machines: false });
  });

  it('permanently deletes an archived account only when explicitly requested', async () => {
    mockFetch().mockResolvedValue(jsonResponse({ status: 'permanently_deleted' }));
    await deleteProviderConfig('opencode', 'alice@example.com', true);
    const [path, init] = lastCall();
    expect(path).toBe('/api/v1/system/provider-config/opencode/alice%40example.com?permanent=true');
    expect(init.method).toBe('DELETE');

    mockFetch().mockClear();
    mockFetch().mockResolvedValue(jsonResponse({ status: 'deleted' }));
    await deleteProviderConfig('opencode', 'alice@example.com');
    expect(lastCall()[0]).toBe('/api/v1/system/provider-config/opencode/alice%40example.com');
  });

  it('getDashboardLayout hits the dashboard-layout path', async () => {
    const payload = { order: ['claude', 'chatgpt'] };
    mockFetch().mockResolvedValue(jsonResponse(payload));
    const data = await getDashboardLayout();
    expect(data).toEqual(payload);
    expect(lastCall()[0]).toBe('/api/v1/system/dashboard-layout');
  });

  it('fetchTokenHealth hits the token-health path', async () => {
    const payload = { tokens: [{ provider: 'claude', account_id: 'default' }] };
    mockFetch().mockResolvedValue(jsonResponse(payload));
    const data = await fetchTokenHealth();
    expect(data).toEqual(payload);
    expect(lastCall()[0]).toBe('/api/v1/system/token-health');
  });

  it('fetchWebhooks hits the webhooks path', async () => {
    const payload = { webhooks: [{ id: 1 }] };
    mockFetch().mockResolvedValue(jsonResponse(payload));
    const data = await fetchWebhooks();
    expect(data).toEqual(payload);
    expect(lastCall()[0]).toBe('/api/v1/system/webhooks');
  });

  it('initGitHubOAuth hits the github init path', async () => {
    const payload = { device_code: 'abc', user_code: 'WXYZ' };
    mockFetch().mockResolvedValue(jsonResponse(payload));
    const data = await initGitHubOAuth();
    expect(data).toEqual(payload);
    expect(lastCall()[0]).toBe('/api/v1/auth/github/init');
  });

  it('getGitHubOAuthStatus hits the github status path', async () => {
    const payload = { authenticated: true };
    mockFetch().mockResolvedValue(jsonResponse(payload));
    const data = await getGitHubOAuthStatus();
    expect(data).toEqual(payload);
    expect(lastCall()[0]).toBe('/api/v1/auth/github/status');
  });

  // --- POST wrappers ---

  it('checkForUpdates POSTs to check-updates', async () => {
    const payload = { server: { current: '2.0.0' } };
    mockFetch().mockResolvedValue(jsonResponse(payload));
    const data = await checkForUpdates();
    expect(data).toEqual(payload);
    const [path, init] = lastCall();
    expect(path).toBe('/api/v1/system/check-updates');
    expect(init.method).toBe('POST');
  });

  it('forceCollect POSTs to force-collect', async () => {
    const payload = { ok: true, cards: 3, sidecars_triggered: 1 };
    mockFetch().mockResolvedValue(jsonResponse(payload));
    const data = await forceCollect();
    expect(data).toEqual(payload);
    const [path, init] = lastCall();
    expect(path).toBe('/api/v1/system/force-collect');
    expect(init.method).toBe('POST');
  });

  it('assignPendingUsageEvents POSTs selected event ids and account', async () => {
    mockFetch().mockResolvedValue(jsonResponse({ assigned: 2, provider_id: 'xai' }));
    await assignPendingUsageEvents([12, 13], 'alice@example.com');
    const [path, init] = lastCall();
    expect(path).toBe('/api/v1/fleet/events/pending/assign');
    expect(init.method).toBe('POST');
    expect(init.body).toBe(JSON.stringify({ event_ids: [12, 13], account_id: 'alice@example.com' }));
  });

  it('assignPendingUsageEventsBatch POSTs provider-specific account assignments', async () => {
    mockFetch().mockResolvedValue(jsonResponse({
      assigned: 3,
      providers: ['anthropic', 'xai'],
      mappings: [
        { provider_id: 'anthropic', sidecar_id: 'host-a', account_id: 'alice@example.com' },
        { provider_id: 'xai', sidecar_id: 'host-b', account_id: 'bob@example.com' },
      ],
    }));
    const assignments = [
      { event_ids: [12, 13], account_id: 'alice@example.com' },
      { event_ids: [21], account_id: 'bob@example.com' },
    ];
    await assignPendingUsageEventsBatch(assignments);
    const [path, init] = lastCall();
    expect(path).toBe('/api/v1/fleet/events/pending/assign-batch');
    expect(init.method).toBe('POST');
    expect(init.body).toBe(JSON.stringify({ assignments }));
  });

  it('logoutGitHub POSTs to github logout', async () => {
    mockFetch().mockResolvedValue(jsonResponse({ ok: true }));
    const data = await logoutGitHub();
    expect(data).toEqual({ ok: true });
    const [path, init] = lastCall();
    expect(path).toBe('/api/v1/auth/github/logout');
    expect(init.method).toBe('POST');
  });

  it('postWake POSTs to wake and resolves to undefined', async () => {
    mockFetch().mockResolvedValue(jsonResponse({ ok: true }));
    await expect(postWake()).resolves.toBeUndefined();
    const [path, init] = lastCall();
    expect(path).toBe('/api/v1/system/wake');
    expect(init.method).toBe('POST');
  });

  it('postWake swallows errors and never rejects', async () => {
    mockFetch().mockRejectedValue(new TypeError('failed to fetch'));
    await expect(postWake()).resolves.toBeUndefined();
  });

  // --- PATCH wrapper with body + interpolated id ---

  it('updateWebhook PATCHes the id path with a JSON body', async () => {
    mockFetch().mockResolvedValue(jsonResponse({ status: 'ok' }));
    const body = { threshold_pct: 90, active: false };
    const data = await updateWebhook(7, body);
    expect(data).toEqual({ status: 'ok' });
    const [path, init] = lastCall();
    expect(path).toBe('/api/v1/system/webhooks/7');
    expect(init.method).toBe('PATCH');
    expect(init.body).toBe(JSON.stringify(body));
    const headers = init.headers as Headers;
    expect(headers.get('Content-Type')).toBe('application/json');
  });
});
