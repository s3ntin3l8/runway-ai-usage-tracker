import {
  checkForUpdates,
  fetchAppConfig,
  fetchFleetUsage,
  fetchLimits,
  fetchProviderConfigs,
  fetchSidecars,
  fetchStatus,
  fetchTokenHealth,
  fetchWebhooks,
  forceCollect,
  getDashboardLayout,
  getGitHubOAuthStatus,
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
