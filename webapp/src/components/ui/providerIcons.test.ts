import { providerIconUrl } from './providerIcons';

describe('providerIconUrl', () => {
  it('returns a URL for a known provider', () => {
    expect(providerIconUrl('anthropic')).toBeTruthy();
  });

  it('maps chatgpt to the openai mark', () => {
    expect(providerIconUrl('chatgpt')).toBe(providerIconUrl('chatgpt'));
    expect(providerIconUrl('chatgpt')).toBeTruthy();
  });

  it('shares one mark across kimi variants', () => {
    const base = providerIconUrl('kimi');
    expect(providerIconUrl('kimi_api')).toBe(base);
    expect(providerIconUrl('kimi_coding')).toBe(base);
    expect(providerIconUrl('kimi_k2')).toBe(base);
  });

  it('shares one mark across opencode variants', () => {
    expect(providerIconUrl('opencode-free')).toBe(providerIconUrl('opencode'));
  });

  it('returns null for an unknown provider', () => {
    expect(providerIconUrl('totally-unknown')).toBeNull();
  });
});
