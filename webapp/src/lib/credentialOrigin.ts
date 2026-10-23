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
 *
 * An Anthropic cookie origin carries a private browser profile (#672): it is
 * named by its client instead, so no profile is ever read out of it.
 */
export function cookieOriginDetail(origin: string): string | undefined {
  const rest = origin.slice('cookie:'.length).split('#')[0];
  const parts = rest.split('/').filter(Boolean);
  if (parts[0] === 'anthropic' || parts[0] === 'anthropic_console') return undefined;
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

/** The clients whose native acceptance covers their cookies (#672, #683). */
const NATIVE_COOKIE_CLIENTS = ['Chrome', 'Safari', 'Claude Desktop'];

/**
 * Credential-store entries whose owning tool is known.
 *
 * The key is the entry's tail - the Keychain service and, where the store
 * needs one, the account: "gemini:antigravity" is the Antigravity CLI's
 * login, in the macOS login Keychain and in the Windows Credential Manager
 * alike.
 */
const KEYCHAIN_ENTRY_OWNERS: Record<string, string | undefined> = {
  'gemini:antigravity': 'Antigravity CLI',
};

/** Describe a raw credential origin for display. Never exposes a secret. */
export function describeCredentialOrigin(origin: string): CredentialOriginDescription {
  if (origin.startsWith('cookie:')) {
    // Same rule as the server: a native Anthropic cookie is named by its client
    // ("Session cookie"), and its private profile never becomes a detail. The
    // segments are split exactly as the server splits them (empty ones count).
    const segments = origin.split('#')[0].split('/');
    const provider = segments[0]?.replace(/^cookie:/, '');
    const native =
      segments.length >= 4 &&
      (provider === 'anthropic' || provider === 'anthropic_console') &&
      NATIVE_COOKIE_CLIENTS.includes(segments[1]);
    return {
      kind: 'cookie',
      label: native ? 'Session cookie' : 'Browser cookie',
      detail: native ? undefined : cookieOriginDetail(origin),
    };
  }
  if (origin.startsWith('keychain:')) {
    const service = origin.slice('keychain:'.length).split('#')[0];
    if (service.startsWith('Claude Desktop/oauth/')) {
      // The app already says "Claude Desktop"; a detail would only repeat it.
      return { kind: 'keychain', label: 'Desktop OAuth' };
    }
    // The app name already identifies the entry ("Claude Code - Keychain
    // entry"); only an unnamed owner needs the service spelled out.
    const app =
      KEYCHAIN_ENTRY_OWNERS[service] ??
      (service.startsWith('Claude Code') ? 'Claude Code' : undefined);
    return {
      kind: 'keychain',
      label: 'Keychain entry',
      detail: app ? undefined : service || undefined,
    };
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
    // The server reads the file name from the path's segments, and a segment
    // separator is a backslash on Windows: "C:\Users\u\.codex\auth.json" is
    // "auth.json", not the whole path.
    const name =
      path.replace(/\\/g, '/').split('/').filter(Boolean).pop() ?? 'Credential file';
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
