// Which CLI logins a sidecar's keep-alive renews. Mirrors KEEP_ALIVE_LABELS in
// app/services/refresh_policy.py (tests/unit/test_keep_alive_copy_contract.py keeps the two in sync).
export const KEEP_ALIVE_LOGINS = ['Antigravity (agy)', 'xAI (Grok)'] as const;

/** "Antigravity (agy) and xAI (Grok)" — human list for tooltips. */
export function keepAliveLoginsText(): string {
  const names = [...KEEP_ALIVE_LOGINS];
  if (names.length <= 1) return names.join('');
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}
