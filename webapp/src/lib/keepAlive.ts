// Which CLI logins a sidecar's keep-alive renews. Mirrors KEEP_ALIVE_LABELS in
// app/services/refresh_policy.py (tests/unit/test_keep_alive_copy_contract.py keeps the two in sync).
export const KEEP_ALIVE_LOGINS = ['Antigravity (agy)', 'Claude Code', 'Codex (ChatGPT)', 'xAI (Grok)'] as const;

/** "Antigravity (agy), Claude Code, Codex (ChatGPT) and xAI (Grok)" — human list for tooltips. */
export function keepAliveLoginsText(): string {
  const names = [...KEEP_ALIVE_LOGINS];
  if (names.length <= 1) return names.join('');
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

export interface KeepAliveInput {
  /** What the sidecar runs now; null = it doesn't report keep-alive (tray app / older build). */
  reported: boolean | null;
  /** The operator's server-side override; null = none (the sidecar's own setting applies). */
  desired: boolean | null;
  /** The sidecar hasn't checked in recently, so a change only lands when it reconnects. */
  offline: boolean;
}

export interface KeepAliveState {
  /** The value that will hold once everything is applied: the override first, else reported. */
  effective: boolean;
  hasOverride: boolean;
  /** An override the sidecar has not confirmed yet (differs from, or was never reported). */
  pending: boolean;
  /** The sidecar can't do keep-alive and nothing is queued for it. */
  unsupported: boolean;
  /** Full sentence for the Fleet card. */
  status: string;
  /** Compact text for a table row. */
  shortStatus: string;
}

const word = (on: boolean): string => (on ? 'on' : 'off');

/**
 * One reading of reported vs desired keep-alive, shared by the Fleet card and the Credentials row
 * so they never describe the same sidecar differently.
 */
export function keepAliveState({ reported, desired, offline }: KeepAliveInput): KeepAliveState {
  const hasOverride = desired !== null;
  const effective = desired ?? reported === true;
  const pending = hasOverride && (reported === null || desired !== reported);
  const unsupported = reported === null && !hasOverride;
  const offlineNote = offline ? ' Sidecar is offline — it applies when it reconnects.' : '';

  if (unsupported) {
    return {
      effective,
      hasOverride,
      pending,
      unsupported,
      status: "This sidecar doesn't report keep-alive (tray app or older build).",
      shortStatus: 'unavailable — update sidecar',
    };
  }
  if (pending) {
    const asked = word(desired as boolean);
    const unconfirmed =
      reported === null
        ? ` — waiting for the sidecar to confirm (the tray app and older builds don't support keep-alive)`
        : ' — applies on next check-in';
    return {
      effective,
      hasOverride,
      pending,
      unsupported,
      status: `Requested ${asked}${unconfirmed}.${offlineNote}`,
      shortStatus: `requested ${asked} — ${reported === null ? 'unconfirmed' : 'pending'}`,
    };
  }
  return {
    effective,
    hasOverride,
    pending,
    unsupported,
    status: `${effective ? 'On — this sidecar renews its logins itself.' : 'Off.'}${offlineNote}`,
    shortStatus: word(effective),
  };
}
