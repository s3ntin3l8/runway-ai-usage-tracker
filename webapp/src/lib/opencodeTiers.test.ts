import { describe, expect, it } from 'vitest';
import { accountConfigProviderIdForUsage } from './opencodeTiers';

describe('accountConfigProviderIdForUsage', () => {
  it.each(['opencode-free', 'opencode-zen', 'hermes-auto'])('maps %s to the shared OpenCode config', (providerId) => {
    expect(accountConfigProviderIdForUsage(providerId)).toBe('opencode');
  });

  it('maps hermes-xai-oauth to xai config', () => {
    expect(accountConfigProviderIdForUsage('hermes-xai-oauth')).toBe('xai');
  });

  it('preserves provider IDs without a shared config alias', () => {
    expect(accountConfigProviderIdForUsage('opencode')).toBe('opencode');
    expect(accountConfigProviderIdForUsage('xai')).toBe('xai');
  });
});
