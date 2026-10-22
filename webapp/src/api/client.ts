// Centralized fetch wrapper. Auth now rides on the HttpOnly `runway_session`
// cookie minted by /auth/session (sent via credentials:'include' below), so
// the admin key no longer needs to live in JS-readable storage.
//
// The `runway_admin_key` localStorage slot is retained read-only for one
// transition: BootGate exchanges any leftover key (from the v1 UI / pre-cookie
// builds) for a session cookie, then clears it. Until then the X-Admin-Key
// header is injected as a fallback. Local-only deployments need no key at all —
// the server's localhost-trust gate applies.

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
    // Set when the response was actually an opaque redirect (see below) — an
    // upstream forward-auth proxy (Authentik/oauth2-proxy/Authelia in front of
    // Runway) bouncing the request to its login page because the SSO session
    // is gone. Distinct from a genuine backend outage: retrying won't help,
    // and the UI should offer to sign in again, not report "unreachable".
    public readonly authRedirect: boolean = false,
    // The parsed `detail` of the error body, when the server sent a
    // structured one — FastAPI's `{"detail": {...}}`. The message above is
    // the human rendering of that body; this is the payload itself, so a
    // caller that needs a field out of it (the billing model a 409 proved,
    // say) reads it here instead of re-parsing a string.
    public readonly detail?: unknown,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

let authRedirectInProgress = false;

export function setAuthRedirectInProgress(value: boolean): void {
  authRedirectInProgress = value;
}

export function isAuthRedirectInProgress(): boolean {
  return authRedirectInProgress;
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  // Fast fail when an auth-redirect / reload is already in flight: do not dispatch
  // any new HTTP requests that could race or clobber the gateway's forward-auth state cookie.
  if (authRedirectInProgress) {
    throw new ApiError(0, 'Authentication required', true);
  }

  const headers = new Headers(init.headers);
  const key = getAdminKey();
  if (key) headers.set('X-Admin-Key', key);
  if (init.body !== undefined && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }

  let resp: Response;
  try {
    // credentials:'include' sends the HttpOnly `runway_session` cookie minted
    // by /auth/session — the primary auth path now that the key no longer
    // lives in localStorage. The X-Admin-Key header above is a transitional
    // fallback for not-yet-migrated logins and API/script clients.
    //
    // redirect:'manual' so a same-site-fronting SSO proxy's 302 to its login
    // page comes back as an opaque `type: 'redirect'` response instead of the
    // browser silently following it cross-origin (where it would just fail
    // CORS). Runway's own backend never issues redirects, so any redirect we
    // see here is unambiguously an upstream auth bounce, not app behavior.
    resp = await fetch(path, { ...init, headers, credentials: 'include', redirect: 'manual' });
  } catch {
    throw new ApiError(0, 'Network error — unable to reach server');
  }

  // Invariant this relies on: Runway's own backend must never issue a real
  // 3xx. If that ever changes, this would misread a legitimate app redirect
  // as a lost SSO session.
  if (resp.type === 'opaqueredirect') {
    throw new ApiError(0, 'Authentication required', true);
  }

  // Same-origin variant: some gateways resolve an expired session with an interstitial
  // HTML page (e.g. 200 OK or 401 Unauthorized with HTML body) rather than an opaque 3xx.
  // Invariant this relies on: Runway's own backend API never returns HTML (always JSON or 204).
  // If documentation/swagger endpoints are ever exposed under this client, this assumption
  // would need to be scoped.
  const contentType = resp.headers.get('content-type') ?? '';
  if (contentType.includes('text/html')) {
    throw new ApiError(0, 'Authentication required', true);
  }

  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    let parsedDetail: unknown;
    try {
      const data = (await resp.json()) as { detail?: unknown };
      if (data && data.detail) {
        parsedDetail = data.detail;
        // A structured detail must not reach the operator as
        // "[object Object]" — that is what `String(detail)` yields for an
        // object. Render it as JSON; a caller that understands the shape
        // reads `err.detail` instead of parsing this message.
        detail =
          typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
      }
    } catch {
      // non-JSON error body — keep the status message
    }
    throw new ApiError(resp.status, detail, false, parsedDetail);
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
