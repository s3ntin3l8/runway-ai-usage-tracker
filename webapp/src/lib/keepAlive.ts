import { useQuery } from '@tanstack/react-query';

import { fetchAppConfig } from '@/api/endpoints';

// Which CLI logins a sidecar's keep-alive renews. Mirrors KEEP_ALIVE_LABELS in
// app/services/refresh_policy.py (tests/unit/test_keep_alive_copy_contract.py keeps the two in sync).
export const KEEP_ALIVE_LOGINS = ['Antigravity (agy)', 'Claude Code', 'Codex (ChatGPT)', 'xAI (Grok)'] as const;

/** Provider id -> the login's name. Mirrors KEEP_ALIVE_LABELS (the contract test keeps them equal). */
export const KEEP_ALIVE_PROVIDER_LABELS = {
  antigravity: 'Antigravity (agy)',
  anthropic: 'Claude Code',
  chatgpt: 'Codex (ChatGPT)',
  xai: 'xAI (Grok)',
} as const;

export type KeepAliveProvider = keyof typeof KEEP_ALIVE_PROVIDER_LABELS;

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
  /**
   * The fleet-wide default (Settings → System): keep-alive on for sidecars without an override.
   * It only ever turns keep-alive on, and only for a sidecar that reports keep-alive at all.
   */
  fleetDefault?: boolean;
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
export function keepAliveState({
  reported,
  desired,
  offline,
  fleetDefault = false,
}: KeepAliveInput): KeepAliveState {
  const hasOverride = desired !== null;
  // The fleet default applies to a sidecar that reports keep-alive and has no override of its own.
  const byDefault = !hasOverride && fleetDefault && reported !== null;
  const effective = desired ?? (reported === true || byDefault);
  const defaultPending = byDefault && reported === false;
  const pending = defaultPending || (hasOverride && (reported === null || desired !== reported));
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
  if (defaultPending) {
    return {
      effective,
      hasOverride,
      pending,
      unsupported,
      status: `On by the fleet default — applies on next check-in.${offlineNote}`,
      shortStatus: 'on (fleet default) — pending',
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
    status: `${effective ? 'On — this sidecar renews its logins itself.' : 'Off.'}${
      byDefault && reported === true ? ' (fleet default)' : ''
    }${offlineNote}`,
    shortStatus: word(effective),
  };
}

/** The fleet-wide keep-alive default from Settings → System (shares BootGate's cached query). */
export function useKeepAliveFleetDefault(): boolean {
  const { data } = useQuery({ queryKey: ['system', 'app-config'], queryFn: fetchAppConfig });
  return data?.sidecar_keep_alive_default === true;
}
