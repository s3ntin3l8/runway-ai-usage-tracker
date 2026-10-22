import { describe, expect, it } from 'vitest';
import { reloadPage } from './reload';

describe('reloadPage', () => {
  it('does not throw', () => {
    // jsdom logs "Not implemented: navigation to another Document" and
    // returns; it does not throw. That is enough to cover the one line the
    // module exists for.
    expect(() => reloadPage()).not.toThrow();
  });
});
