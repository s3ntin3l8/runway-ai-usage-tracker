// Centralized fetch wrapper injecting the admin key header.
//
// The key lives in localStorage (same `runway_admin_key` slot as the v1 UI,
// so existing logins carry over). sessionStorage wouldn't meaningfully
// reduce XSS impact and forces re-login per tab; local-only deployments
// don't need a key at all — the server's localhost-trust gate applies.

const ADMIN_KEY_STORAGE = 'runway_admin_key';

export function getAdminKey(): string | null {
  return localStorage.getItem(ADMIN_KEY_STORAGE);
}

export function setAdminKey(key: string): void {
  localStorage.setItem(ADMIN_KEY_STORAGE, key);
}

export function clearAdminKey(): void {
  localStorage.removeItem(ADMIN_KEY_STORAGE);
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const key = getAdminKey();
  if (key) headers.set('X-Admin-Key', key);
  if (init.body !== undefined && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }

  let resp: Response;
  try {
    resp = await fetch(path, { ...init, headers });
  } catch {
    throw new ApiError(0, 'Network error — unable to reach server');
  }

  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const data = (await resp.json()) as { detail?: unknown };
      if (data && data.detail) detail = String(data.detail);
    } catch {
      // non-JSON error body — keep the status message
    }
    throw new ApiError(resp.status, detail);
  }

  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

type QueryValue = string | number | boolean | null | undefined;

// Build "?a=1&b=x" from params, skipping null/undefined values.
export function qs(params: Record<string, QueryValue>): string {
  const search = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === null || v === undefined) continue;
    search.set(k, String(v));
  }
  const s = search.toString();
  return s ? `?${s}` : '';
}
