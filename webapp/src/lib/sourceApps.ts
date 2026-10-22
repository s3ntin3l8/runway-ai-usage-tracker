// Human-readable labels for the client apps the sidecar reports in `source_app`.
//
// `source_app` names the client that made a call, which is not the same as
// the provider it was billed to: MiniMax Code, OpenCode and a plain API
// script can all spend the same MiniMax plan. Unknown ids fall back to the
// raw value, so a newly-seen app still renders (unformatted) before its
// label is added here.

const SOURCE_APP_LABELS: Record<string, string> = {
  antigravity: 'Antigravity',
  claude_code: 'Claude Code',
  codex: 'Codex',
  gemini_cli: 'Gemini CLI',
  hermes: 'Hermes',
  minimax_code: 'MiniMax Code',
  opencode: 'OpenCode',
  xai_cli: 'Grok CLI',
};

export function sourceAppLabel(app: string): string {
  return SOURCE_APP_LABELS[app] ?? app;
}
