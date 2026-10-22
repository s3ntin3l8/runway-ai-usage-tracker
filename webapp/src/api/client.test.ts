import {
  api,
  ApiError,
  clearAdminKey,
  getAdminKey,
  qs,
  setAdminKey,
  setAuthRedirectInProgress,
} from './client';

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
}

describe('admin key storage', () => {
  beforeEach(() => localStorage.clear());

  it('get returns null when unset, set persists, clear removes', () => {
    expect(getAdminKey()).toBeNull();
    setAdminKey('secret');
    expect(getAdminKey()).toBe('secret');
    expect(localStorage.getItem('runway_admin_key')).toBe('secret');
    clearAdminKey();
    expect(getAdminKey()).toBeNull();
  });
});

describe('qs', () => {
  it('builds a query string, skipping null/undefined', () => {
    expect(qs({ a: 1, b: 'x', c: true, d: null, e: undefined })).toBe('?a=1&b=x&c=true');
  });

  it('returns an empty string when no params survive', () => {
    expect(qs({ a: null, b: undefined })).toBe('');
    expect(qs({})).toBe('');
  });
});

describe('api', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.stubGlobal('fetch', vi.fn());
  });
  afterEach(() => vi.unstubAllGlobals());

  function mockFetch() {
    return fetch as unknown as ReturnType<typeof vi.fn>;
  }

  it('fetches the path and parses JSON', async () => {
    mockFetch().mockResolvedValue(jsonResponse({ ok: true }));
    const data = await api<{ ok: boolean }>('/api/v1/thing');
    expect(data).toEqual({ ok: true });
    expect(mockFetch()).toHaveBeenCalledWith('/api/v1/thing', expect.any(Object));
  });

  it('does not send the admin-key header when no key is stored', async () => {
    mockFetch().mockResolvedValue(jsonResponse({}));
    await api('/api/v1/thing');
    const headers = mockFetch().mock.calls[0][1].headers as Headers;
    expect(headers.has('X-Admin-Key')).toBe(false);
  });

  it('injects the X-Admin-Key header after setAdminKey', async () => {
    setAdminKey('topsecret');
    mockFetch().mockResolvedValue(jsonResponse({}));
    await api('/api/v1/thing');
    const headers = mockFetch().mock.calls[0][1].headers as Headers;
    expect(headers.get('X-Admin-Key')).toBe('topsecret');
  });

  it('sets a JSON Content-Type when a body is provided', async () => {
    mockFetch().mockResolvedValue(jsonResponse({}));
    await api('/api/v1/thing', { method: 'POST', body: JSON.stringify({ a: 1 }) });
    const headers = mockFetch().mock.calls[0][1].headers as Headers;
    expect(headers.get('Content-Type')).toBe('application/json');
  });

  it('keeps a caller-provided Content-Type', async () => {
    mockFetch().mockResolvedValue(jsonResponse({}));
    await api('/api/v1/thing', {
      method: 'POST',
      body: 'raw',
      headers: { 'Content-Type': 'text/plain' },
    });
    const headers = mockFetch().mock.calls[0][1].headers as Headers;
    expect(headers.get('Content-Type')).toBe('text/plain');
  });

  it('returns undefined for a 204 response', async () => {
    mockFetch().mockResolvedValue(new Response(null, { status: 204 }));
    await expect(api('/api/v1/thing')).resolves.toBeUndefined();
  });

  it('throws an ApiError with the JSON detail on a non-ok response', async () => {
    mockFetch().mockResolvedValue(
      jsonResponse({ detail: 'nope' }, { status: 403 }),
    );
    await expect(api('/api/v1/thing')).rejects.toMatchObject({
      name: 'ApiError',
      status: 403,
      message: 'nope',
    });
  });

  it('carries a structured error detail and never renders it as [object Object]', async () => {
    // FastAPI sends `{"detail": {...}}` for some errors. The payload is
    // kept so a caller can read a field out of it, and the message is a
    // readable rendering of it — never "[object Object]".
    mockFetch().mockResolvedValue(
      jsonResponse({ detail: { suggested_billing_type: 'subscription' } }, { status: 409 }),
    );
    const err = (await api('/api/v1/thing').catch((e) => e)) as ApiError;
    expect(err.status).toBe(409);
    expect(err.detail).toEqual({ suggested_billing_type: 'subscription' });
    expect(err.message).not.toMatch(/\[object Object\]/);
    expect(err.message).toContain('subscription');
  });

  it('summarises a validation error without echoing the submitted value', async () => {
    // FastAPI repeats the offending input under `input`. The message
    // reaches every toast in the app, so it names the field and the
    // problem — never the value the operator submitted.
    mockFetch().mockResolvedValue(
      jsonResponse(
        {
          detail: [
            {
              type: 'string_too_short',
              loc: ['body', 'api_key'],
              msg: 'String should have at least 1 character',
              input: 'sk-ant-supersecret', // pragma: allowlist secret
            },
          ],
        },
        { status: 422 },
      ),
    );
    const err = (await api('/api/v1/thing').catch((e) => e)) as ApiError;
    expect(err.message).toContain('body.api_key');
    expect(err.message).toContain('String should have at least 1 character');
    expect(err.message).not.toContain('sk-ant-supersecret');
    // The body itself is still there for a caller that needs the field.
    expect(err.detail).toEqual([
      {
        type: 'string_too_short',
        loc: ['body', 'api_key'],
        msg: 'String should have at least 1 character',
        input: 'sk-ant-supersecret', // pragma: allowlist secret
      },
    ]);
  });

  it('uses the message a structured error carries', async () => {
    mockFetch().mockResolvedValue(
      jsonResponse(
        { detail: { message: 'Merge has duplicate history.', collisions: 2 } },
        { status: 409 },
      ),
    );
    const err = (await api('/api/v1/thing').catch((e) => e)) as ApiError;
    expect(err.message).toBe('Merge has duplicate history.');
  });

  it('cuts a long error body short', async () => {
    mockFetch().mockResolvedValue(
      jsonResponse({ detail: { note: 'x'.repeat(500) } }, { status: 500 }),
    );
    const err = (await api('/api/v1/thing').catch((e) => e)) as ApiError;
    expect(err.message.length).toBeLessThanOrEqual(301);
    expect(err.message.endsWith('…')).toBe(true);
  });

  it('falls back to the HTTP status when the error body is not JSON', async () => {
    mockFetch().mockResolvedValue(
      new Response('boom', { status: 500 }),
    );
    await expect(api('/api/v1/thing')).rejects.toMatchObject({
      status: 500,
      message: 'HTTP 500',
    });
  });

  it('wraps a fetch rejection as a network ApiError', async () => {
    mockFetch().mockRejectedValue(new TypeError('failed to fetch'));
    const err = (await api('/api/v1/thing').catch((e) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(0);
    expect(err.message).toMatch(/network error/i);
  });

  it('requests redirect:"manual" so an upstream SSO bounce surfaces instead of being followed', async () => {
    mockFetch().mockResolvedValue(jsonResponse({}));
    await api('/api/v1/thing');
    expect(mockFetch().mock.calls[0][1]).toMatchObject({ redirect: 'manual' });
  });

  it('flags an opaque redirect as an authRedirect ApiError, not a network outage', async () => {
    // fetch resolves (doesn't reject) when redirect:'manual' hits a 3xx — the
    // response comes back with type:'opaqueredirect' instead of being followed.
    mockFetch().mockResolvedValue({ type: 'opaqueredirect' });
    const err = (await api('/api/v1/thing').catch((e) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.authRedirect).toBe(true);
    expect(err.message).toMatch(/authentication required/i);
  });

  it('flags a 200 text/html response from an SSO gateway as an authRedirect ApiError', async () => {
    mockFetch().mockResolvedValue(
      new Response('<html>login</html>', {
        status: 200,
        headers: { 'Content-Type': 'text/html; charset=utf-8' },
      }),
    );
    const err = (await api('/api/v1/thing').catch((e) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.authRedirect).toBe(true);
    expect(err.message).toMatch(/authentication required/i);
  });

  it('flags a 401 text/html response from an SSO gateway as an authRedirect ApiError', async () => {
    mockFetch().mockResolvedValue(
      new Response('<html>login required</html>', {
        status: 401,
        headers: { 'Content-Type': 'text/html; charset=utf-8' },
      }),
    );
    const err = (await api('/api/v1/thing').catch((e) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.authRedirect).toBe(true);
    expect(err.message).toMatch(/authentication required/i);
  });

  it('fast-fails when authRedirectInProgress is active', async () => {
    setAuthRedirectInProgress(true);
    try {
      const err = (await api('/api/v1/thing').catch((e) => e)) as ApiError;
      expect(err).toBeInstanceOf(ApiError);
      expect(err.authRedirect).toBe(true);
      expect(mockFetch()).not.toHaveBeenCalled();
    } finally {
      setAuthRedirectInProgress(false);
    }
  });
});
