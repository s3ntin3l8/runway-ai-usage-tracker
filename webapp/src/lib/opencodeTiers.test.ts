import { describe, expect, it } from 'vitest';
import { accountConfigProviderIdForUsage } from './opencodeTiers';

describe('accountConfigProviderIdForUsage', () => {
  it.each(['opencode-free', 'opencode-zen', 'hermes-auto'])('maps %s to the shared OpenCode config', (providerId) => {
    expect(accountConfigProviderIdForUsage(providerId)).toBe('opencode');
  });

  it.each(['hermes-xai-oauth', 'xai-oauth', 'xai-api'])('maps %s to xai config', (providerId) => {
    expect(accountConfigProviderIdForUsage(providerId)).toBe('xai');
  });

  it('preserves provider IDs without a shared config alias', () => {
    expect(accountConfigProviderIdForUsage('opencode')).toBe('opencode');
    expect(accountConfigProviderIdForUsage('xai')).toBe('xai');
  });
});
