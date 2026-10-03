import { describe, expect, it } from 'vitest';
import { accountConfigProviderIdForUsage, relatedAccountProviderIds } from './providerAccountAliases';

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

describe('relatedAccountProviderIds', () => {
  it('lets Gemini usage be assigned to Antigravity accounts', () => {
    expect(relatedAccountProviderIds('gemini')).toEqual(['antigravity']);
  });

  it('has no related providers by default', () => {
    expect(relatedAccountProviderIds('xai')).toEqual([]);
  });
});
