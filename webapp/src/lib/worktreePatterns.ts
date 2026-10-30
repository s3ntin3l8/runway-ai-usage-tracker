// Client-side check for a worktree directory pattern (Settings > System).
// Mirrors app/services/project_label.py (the server re-validates).
export const MAX_PATTERNS = 20;
export const MAX_PATTERN_LENGTH = 64;

/** The reason a pattern is not acceptable, or null. */
export function patternError(raw: string, existing: string[]): string | null {
  const p = raw.trim();
  if (!p) return 'Enter a directory name.';
  if (p.length > MAX_PATTERN_LENGTH) return `At most ${MAX_PATTERN_LENGTH} characters.`;
  if (/[\\/]/.test(p)) return 'A single directory name: no path separators.';
  // eslint-disable-next-line no-control-regex
  if (/[\x00-\x1f\x7f]/.test(p)) return 'No control characters.';
  if (/[?[\]]/.test(p)) return "Only '*' is allowed as a wildcard.";
  if (p.replace(/\*/g, '') === '' || p === '.' || p === '..') return 'Include a literal name.';
  if (existing.some((e) => e.toLowerCase() === p.toLowerCase())) return 'Already in the list.';
  if (existing.length >= MAX_PATTERNS) return `At most ${MAX_PATTERNS} patterns.`;
  return null;
}
