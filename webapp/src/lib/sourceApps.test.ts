import { describe, expect, it } from 'vitest';
import { sourceAppLabel } from './sourceApps';

describe('sourceAppLabel', () => {
  it.each([
    ['opencode', 'OpenCode'],
    ['claude_code', 'Claude Code'],
    ['codex', 'Codex'],
    ['gemini_cli', 'Gemini CLI'],
    ['antigravity', 'Antigravity'],
    ['hermes', 'Hermes'],
    ['xai_cli', 'Grok CLI'],
    ['minimax_code', 'MiniMax Code'],
  ])('labels %s', (app, label) => {
    expect(sourceAppLabel(app)).toBe(label);
  });

  it('falls back to the raw id for an app it has no label for', () => {
    // A client app can report usage before Runway ships a release that knows
    // its name, so an unknown id still has to render.
    expect(sourceAppLabel('some_new_app')).toBe('some_new_app');
  });
});
