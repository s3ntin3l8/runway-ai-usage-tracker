import { describe, expect, it } from 'vitest';
import { accountConfigProviderIdForUsage } from './opencodeTiers';

describe('accountConfigProviderIdForUsage', () => {
  it.each(['opencode-free', 'opencode-zen'])('maps %s to the shared OpenCode config', (providerId) => {
    expect(accountConfigProviderIdForUsage(providerId)).toBe('opencode');
  });

  it('preserves provider IDs without a shared config alias', () => {
    expect(accountConfigProviderIdForUsage('opencode')).toBe('opencode');
    expect(accountConfigProviderIdForUsage('xai')).toBe('xai');
  });
});
