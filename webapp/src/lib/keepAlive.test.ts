import { KEEP_ALIVE_LOGINS, keepAliveLoginsText } from './keepAlive';

describe('keepAliveLoginsText', () => {
  it('lists every login keep-alive covers', () => {
    const text = keepAliveLoginsText();
    for (const name of KEEP_ALIVE_LOGINS) expect(text).toContain(name);
    expect(text).toBe('Antigravity (agy) and xAI (Grok)');
  });
});
