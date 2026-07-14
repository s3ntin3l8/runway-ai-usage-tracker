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
    expect(providerIconUrl('opencode-byok')).toBe(providerIconUrl('opencode'));
  });

  it('maps opencode sub-providers to their real upstream brand mark', () => {
    expect(providerIconUrl('opencode-openrouter')).toBe(providerIconUrl('openrouter'));
    expect(providerIconUrl('opencode-ollama')).toBe(providerIconUrl('ollama'));
  });

  it('falls back to the opencode mark for an unrecognized opencode-* sub-provider', () => {
    expect(providerIconUrl('opencode-some-new-backend')).toBe(providerIconUrl('opencode'));
  });

  it('returns null for an unknown provider', () => {
    expect(providerIconUrl('totally-unknown')).toBeNull();
  });
});
