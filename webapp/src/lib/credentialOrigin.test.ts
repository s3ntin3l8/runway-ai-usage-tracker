import { describe, expect, it } from 'vitest';
import origins from './credentialOrigins.fixture.json';
import {
  cookieOriginDetail,
  credentialOriginKind,
  describeCredentialOrigin,
  originTitleWithApp,
  originTitleWithDetail,
} from './credentialOrigin';

describe('describeCredentialOrigin', () => {
  it('names a browser cookie as a cookie, not a file', () => {
    const described = describeCredentialOrigin('cookie:opencode/session');
    expect(described.kind).toBe('cookie');
    expect(described.label).toBe('Browser cookie');
    expect(described.path).toBeUndefined();
  });

  it('names the browser and profile a cookie came from', () => {
    expect(describeCredentialOrigin('cookie:opencode/Chrome/Profile 1/Cookies').detail).toBe(
      'Chrome · Profile 1',
    );
  });

  it('never reads out the profile of an Anthropic cookie', () => {
    // An Anthropic cookie carries a private browser profile (#672): it is named
    // by its client instead.
    const native = describeCredentialOrigin('cookie:anthropic/Claude Desktop/Default/Cookies');
    expect(native.label).toBe('Session cookie');
    expect(native.detail).toBeUndefined();
    // A browser native acceptance has not covered yet stays unnamed too.
    const other = describeCredentialOrigin('cookie:anthropic/Firefox/private-profile/Cookies');
    expect(other.label).toBe('Browser cookie');
    expect(other.detail).toBeUndefined();
    expect(describeCredentialOrigin('cookie:anthropic_console/Chrome/private/Cookies').detail).toBeUndefined();
  });

  it('never reads the store folder as a profile', () => {
    // Chromium keeps the cookie file in a `Network` folder on some platforms.
    expect(describeCredentialOrigin('cookie:opencode/Chrome/Network/Cookies').detail).toBe('Chrome');
    expect(describeCredentialOrigin('cookie:opencode/Chrome/Default/Cookies').detail).toBe(
      'Chrome · Default',
    );
  });

  it('never leaks the fingerprint into the description', () => {
    expect(describeCredentialOrigin('cookie:opencode/Chrome/Default/Cookies#abc123').detail).toBe(
      'Chrome · Default',
    );
  });

  it('describes a file by its name and keeps the path', () => {
    const described = describeCredentialOrigin('path:/home/u/.codex/auth.json#fp');
    expect(described.kind).toBe('file');
    expect(described.label).toBe('auth.json');
    expect(described.path).toBe('/home/u/.codex/auth.json');
  });

  it('normalises a file URI like the server does', () => {
    // Same as the Python twin: only the authority is dropped, the path keeps
    // its leading slash.
    expect(describeCredentialOrigin('file:///home/u/x.json').path).toBe('/home/u/x.json');
    expect(describeCredentialOrigin('file://localhost/home/u/x.json').path).toBe('/home/u/x.json');
    // A Windows drive letter loses the slash before it, like the server does.
    expect(describeCredentialOrigin('file:///C:/Users/u/.codex/auth.json').path).toBe(
      'C:/Users/u/.codex/auth.json',
    );
  });

  it('describes an env var by its name', () => {
    const described = describeCredentialOrigin('env:OPENROUTER_API_KEY');
    expect(described.kind).toBe('env');
    expect(described.label).toBe('OPENROUTER_API_KEY');
  });

  it('describes a keychain entry by its service', () => {
    const described = describeCredentialOrigin('keychain:Gemini CLI');
    expect(described.kind).toBe('keychain');
    expect(described.label).toBe('Keychain entry');
    expect(described.detail).toBe('Gemini CLI');
  });

  it('does not repeat the app name in a keychain detail', () => {
    // The app already says "Claude Desktop" / "Claude Code"; a detail would
    // only repeat it.
    expect(describeCredentialOrigin('keychain:Claude Desktop/oauth/x').detail).toBeUndefined();
    expect(describeCredentialOrigin('keychain:Claude Code-credentials').detail).toBeUndefined();
  });

  it('falls back to a sidecar credential for an origin it cannot place', () => {
    const described = describeCredentialOrigin('provider:anthropic');
    expect(described.kind).toBe('sidecar');
    expect(described.label).toBe('Sidecar credential');
  });
});

describe('cookieOriginDetail', () => {
  it('has nothing to say when the origin names no jar', () => {
    expect(cookieOriginDetail('cookie:opencode/session')).toBeUndefined();
    expect(cookieOriginDetail('path:/home/u/a.json')).toBeUndefined();
  });
});

describe('credentialOriginKind', () => {
  it('tells a cookie from a file', () => {
    expect(credentialOriginKind('cookie:opencode/session')).toBe('cookie');
    expect(credentialOriginKind('path:/home/u/a.json')).toBe('file');
  });
});

describe('the shared origin fixture', () => {
  it('describes every origin the way the server does', () => {
    // The server parses origins with its own twin of this parser
    // (app/services/credential_sources.py). One list of origins pins both
    // sides, so a change to one shows up as a failing test here instead of as
    // a review comment.
    for (const { origin, kind, label, detail } of origins) {
      const described = describeCredentialOrigin(origin);
      expect({
        kind: described.kind,
        label: described.label,
        detail: described.detail ?? null,
      }).toEqual({ kind, label, detail });
    }
  });
});

describe('originTitleWithApp', () => {
  it('names the app, unless it adds nothing', () => {
    expect(originTitleWithApp('Codex CLI', 'auth.json')).toBe('Codex CLI · auth.json');
    // A generic browser says nothing.
    expect(originTitleWithApp('Browser', 'Browser cookie')).toBe('Browser cookie');
    // An app the label already names is not repeated.
    expect(originTitleWithApp('Claude Desktop', 'Claude Desktop OAuth')).toBe(
      'Claude Desktop OAuth',
    );
    // A label that is the app's name is not repeated either.
    expect(originTitleWithApp('Chrome', 'Chrome')).toBe('Chrome');
    // Only as a word: "Chromebook cookie" does not name "Chrome".
    expect(originTitleWithApp('Chrome', 'Chromebook cookie')).toBe(
      'Chrome · Chromebook cookie',
    );
    // No app at all.
    expect(originTitleWithApp(undefined, 'Keychain entry')).toBe('Keychain entry');
  });
});

describe('originTitleWithDetail', () => {
  it('joins the detail in parentheses', () => {
    expect(originTitleWithDetail('Browser cookie', 'Chrome · Profile 1')).toBe(
      'Browser cookie (Chrome · Profile 1)',
    );
    // No detail, nothing to add.
    expect(originTitleWithDetail('Browser cookie', null)).toBe('Browser cookie');
  });
});
