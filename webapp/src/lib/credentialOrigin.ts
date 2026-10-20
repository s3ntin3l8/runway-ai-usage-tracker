// Describe a raw credential origin ("cookie:opencode/session") in words.
//
// The server sends the same description on its credential views
// (`origin_label` / `origin_detail`); this module is the fallback for the
// places that only ever see the raw origin string — the pending-tag dialog
// and the "credentials without an account" list. Both sides use the same
// vocabulary so a credential reads the same wherever it shows up.

export type CredentialOriginKind = 'cookie' | 'keychain' | 'file' | 'env' | 'sidecar';

export interface CredentialOriginDescription {
  kind: CredentialOriginKind;
  /** "Browser cookie", "auth.json", "OPENROUTER_API_KEY", … */
  label: string;
  /** Extra context: the browser and profile a cookie came from, a keychain service, … */
  detail?: string;
  /** The file path, for a file origin. */
  path?: string;
}

/** Browsers whose cookie jar a sidecar can read. */
const BROWSER_TOKENS = [
  'chrome',
  'chromium',
  'firefox',
  'safari',
  'edge',
  'brave',
  'arc',
  'opera',
  'vivaldi',
  'librewolf',
  'desktop',
];

/**
 * Whether a path segment names the store itself rather than a profile.
 *
 * Chromium keeps the cookie file in a `Network` folder on some platforms
 * (`<profile>/Network/Cookies`), so that segment belongs to the store path and
 * must never be read as a profile name.
 */
function isCookieStore(part: string): boolean {
  const lowered = part.toLowerCase();
  return (
    lowered.startsWith('cookies') || lowered.endsWith('cookies.sqlite') || lowered === 'network'
  );
}

/**
 * The browser and profile a cookie origin names.
 *
 * `cookie:opencode/Chrome/Profile 1/Cookies` → "Chrome · Profile 1";
 * `cookie:opencode/session` → nothing (the machine could not name the jar).
 */
export function cookieOriginDetail(origin: string): string | undefined {
  const rest = origin.slice('cookie:'.length).split('#')[0];
  const parts = rest.split('/').filter(Boolean);
  for (let index = 1; index < parts.length; index += 1) {
    const part = parts[index];
    const lowered = part.toLowerCase();
    if (!BROWSER_TOKENS.some((token) => lowered.includes(token))) continue;
    const following = parts[index + 1];
    const profile = following && !isCookieStore(following) ? following : undefined;
    return profile ? `${part} · ${profile}` : part;
  }
  return undefined;
}

/** Describe a raw credential origin for display. Never exposes a secret. */
export function describeCredentialOrigin(origin: string): CredentialOriginDescription {
  if (origin.startsWith('cookie:')) {
    return {
      kind: 'cookie',
      label: 'Browser cookie',
      detail: cookieOriginDetail(origin),
    };
  }
  if (origin.startsWith('keychain:')) {
    const service = origin.slice('keychain:'.length).split('#')[0];
    if (service.startsWith('Claude Desktop/oauth/')) {
      // The app already says "Claude Desktop"; a detail would only repeat it.
      return { kind: 'keychain', label: 'Desktop OAuth' };
    }
    return { kind: 'keychain', label: 'Keychain entry', detail: service || undefined };
  }
  if (origin.startsWith('env:')) {
    return { kind: 'env', label: origin.slice('env:'.length).split('#')[0] };
  }
  if (origin.startsWith('path:') || origin.startsWith('file:')) {
    const raw = origin.slice(origin.indexOf(':') + 1).split('#')[0];
    // Same normalisation as the server: "file:///home/u/x.json" keeps its
    // leading slash ("/home/u/x.json"), only the authority is dropped, and a
    // Windows drive letter loses the slash before it ("C:/Users/...").
    const path = raw
      .replace(/^\/{2,}(localhost)?(?=\/)/, '')
      .replace(/^\/(?=[A-Za-z]:)/, '');
    const name = path.split('/').filter(Boolean).pop() ?? 'Credential file';
    return { kind: 'file', label: name, path };
  }
  return { kind: 'sidecar', label: 'Sidecar credential' };
}

/**
 * The source kind of a raw origin — what the credential *is*, independent of
 * where it was found. A cookie is a browser session cookie, not a file.
 */
export function credentialOriginKind(origin: string): CredentialOriginKind {
  return describeCredentialOrigin(origin).kind;
}
