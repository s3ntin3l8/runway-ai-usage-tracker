// Which of a sidecar's settings the sidecar has not applied yet. The status line at the top of the
// settings dialog counts them and each section decides whether to warn from the same functions, so
// the two cannot disagree.

import type { Sidecar, SidecarChannel } from '@/api/types';

export interface FleetUpdateDefaults {
  autoUpdate: boolean;
  channel: SidecarChannel;
}

export interface UpdateDrift {
  effectiveAuto: boolean;
  effectiveChannel: SidecarChannel;
  /** The sidecar reports something other than what Runway asks for (not landed yet, or its own config wins). */
  auto: boolean;
  channel: boolean;
}

export function updateDrift(sidecar: Sidecar, fleet: FleetUpdateDefaults): UpdateDrift {
  const effectiveAuto = sidecar.effective_auto_update ?? fleet.autoUpdate;
  const effectiveChannel = sidecar.effective_update_channel ?? fleet.channel;
  const canSelfUpdate = sidecar.self_update_capable !== false;
  return {
    effectiveAuto,
    effectiveChannel,
    auto: canSelfUpdate && sidecar.auto_update != null && sidecar.auto_update !== effectiveAuto,
    channel: canSelfUpdate && sidecar.update_channel != null && sidecar.update_channel !== effectiveChannel,
  };
}

/** The sidecar logs at a level other than the one requested. A sidecar that never reports one is not lagging. */
export function logLevelLagging(sidecar: Sidecar): boolean {
  const desired = sidecar.log_level_desired ?? null;
  return desired !== null && sidecar.log_level != null && sidecar.log_level !== desired;
}

export function loginDirsPending(sidecar: Sidecar): boolean {
  const report = sidecar.login_dirs_report;
  if (!report) return false;
  const desired = sidecar.login_dirs_desired ?? { claude_config_dirs: [], codex_home: [] };
  return (
    JSON.stringify(report.applied.claude_config_dirs) !== JSON.stringify(desired.claude_config_dirs) ||
    JSON.stringify(report.applied.codex_home) !== JSON.stringify(desired.codex_home)
  );
}

/** Settings that are asked for but not applied yet, across every section of the dialog. */
export function pendingSettings(sidecar: Sidecar, fleet: FleetUpdateDefaults, keepAlivePending: boolean): number {
  const drift = updateDrift(sidecar, fleet);
  return [keepAlivePending, drift.auto, drift.channel, logLevelLagging(sidecar), loginDirsPending(sidecar)].filter(Boolean)
    .length;
}
