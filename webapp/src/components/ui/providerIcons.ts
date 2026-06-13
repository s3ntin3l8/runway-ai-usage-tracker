// Locally-vendored provider brand marks (SVG), served same-origin so the
// strict CSP (img-src 'self') stays intact — no CDN at runtime. Sourced from
// homarr-labs/dashboard-icons (Apache-2.0) and @lobehub/icons (MIT).
//
// We use the dark-ink / light-background variant of each mark and render it on
// a white "app-icon" chip (see ProviderGlyph), which keeps monochrome marks
// (OpenAI, OpenRouter, etc.) visible in both light and dark themes without any
// per-theme swapping. Vite resolves each import to a hashed asset URL.

import anthropic from '@/assets/providers/claude.svg';
import openai from '@/assets/providers/openai.svg';
import gemini from '@/assets/providers/gemini.svg';
import githubCopilot from '@/assets/providers/github-copilot.svg';
import opencode from '@/assets/providers/opencode.svg';
import zai from '@/assets/providers/zai.svg';
import kimi from '@/assets/providers/kimi.svg';
import minimax from '@/assets/providers/minimax.svg';
import openrouter from '@/assets/providers/openrouter.svg';
import ollama from '@/assets/providers/ollama.svg';
import antigravity from '@/assets/providers/antigravity.svg';

// Keyed by provider_id (see app/core/registry.json). Kimi + opencode variants
// share a single brand mark.
const PROVIDER_ICONS: Record<string, string> = {
  anthropic,
  chatgpt: openai,
  gemini,
  github: githubCopilot,
  opencode,
  'opencode-free': opencode,
  zai,
  kimi,
  kimi_api: kimi,
  kimi_coding: kimi,
  kimi_k2: kimi,
  minimax,
  openrouter,
  ollama,
  antigravity,
};

/** Asset URL for a provider's brand mark, or null if we don't vendor one. */
export function providerIconUrl(providerId: string): string | null {
  return PROVIDER_ICONS[providerId] ?? null;
}
