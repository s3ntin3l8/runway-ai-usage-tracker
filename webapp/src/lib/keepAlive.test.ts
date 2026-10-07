import { KEEP_ALIVE_LOGINS, keepAliveLoginsText, keepAliveState } from './keepAlive';

describe('keepAliveLoginsText', () => {
  it('lists every login keep-alive covers', () => {
    const text = keepAliveLoginsText();
    for (const name of KEEP_ALIVE_LOGINS) expect(text).toContain(name);
    expect(text).toBe('Antigravity (agy), Claude Code and xAI (Grok)');
  });
});


describe('keepAliveState', () => {
  const st = (reported: boolean | null, desired: boolean | null, offline = false) =>
    keepAliveState({ reported, desired, offline });

  it('reads the reported value when there is no override', () => {
    expect(st(true, null)).toMatchObject({ effective: true, pending: false, hasOverride: false });
    expect(st(false, null)).toMatchObject({ effective: false, pending: false });
    expect(st(true, null).status).toMatch(/^On — /);
    expect(st(false, null).status).toBe('Off.');
  });

  it('lets the override win and calls it pending until the sidecar confirms it', () => {
    expect(st(false, true)).toMatchObject({ effective: true, pending: true, hasOverride: true });
    expect(st(true, false)).toMatchObject({ effective: false, pending: true });
    expect(st(false, true).status).toBe('Requested on — applies on next check-in.');
  });

  it('is not pending once reported matches the override', () => {
    expect(st(true, true)).toMatchObject({ pending: false, hasOverride: true });
    expect(st(false, false)).toMatchObject({ pending: false, hasOverride: true });
  });

  it('treats an override on a never-reporting sidecar as unconfirmed, not invisible', () => {
    const off = st(null, false);
    expect(off).toMatchObject({ effective: false, pending: true, unsupported: false });
    expect(off.status).toMatch(/Requested off — waiting for the sidecar to confirm/);
    expect(off.shortStatus).toBe('requested off — unconfirmed');
    expect(st(false, true).shortStatus).toBe('requested on — pending');
    expect(st(null, true)).toMatchObject({ effective: true, pending: true });
  });

  it('marks a never-reporting sidecar with no override as unsupported', () => {
    expect(st(null, null)).toMatchObject({ unsupported: true, pending: false, effective: false });
    expect(st(null, null).shortStatus).toMatch(/unavailable — update sidecar/);
  });

  it('adds an offline note to everything except the unsupported case', () => {
    for (const [r, d] of [[true, null], [false, null], [false, true], [null, true]] as const) {
      expect(st(r, d, true).status).toMatch(/Sidecar is offline — it applies when it reconnects/);
      expect(st(r, d, false).status).not.toMatch(/offline/);
    }
    expect(st(null, null, true).status).not.toMatch(/offline/);
  });
});
