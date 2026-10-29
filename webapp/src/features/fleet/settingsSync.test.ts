import type { Sidecar } from '@/api/types';
import { logLevelLagging, loginDirsPending, pendingSettings, updateDrift } from './settingsSync';

const sidecar = (over: Partial<Sidecar> = {}): Sidecar => ({ sidecar_id: 'laptop', ...over }) as Sidecar;
const fleet = { autoUpdate: true, channel: 'stable' as const };

describe('updateDrift', () => {
  it('falls back to the fleet defaults when Runway has no effective value yet', () => {
    expect(updateDrift(sidecar(), fleet)).toMatchObject({ effectiveAuto: true, effectiveChannel: 'stable', auto: false, channel: false });
  });

  it('flags a sidecar that reports something else than Runway asks for', () => {
    const drift = updateDrift(sidecar({ auto_update: false, update_channel: 'edge' }), fleet);
    expect(drift).toMatchObject({ auto: true, channel: true });
  });

  it('is quiet when they agree, when the sidecar has not reported, and for a build that cannot self-update', () => {
    expect(updateDrift(sidecar({ auto_update: true, update_channel: 'stable' }), fleet)).toMatchObject({ auto: false, channel: false });
    expect(updateDrift(sidecar(), fleet)).toMatchObject({ auto: false, channel: false });
    expect(updateDrift(sidecar({ auto_update: false, update_channel: 'edge', self_update_capable: false }), fleet)).toMatchObject({
      auto: false,
      channel: false,
    });
  });
});

describe('logLevelLagging', () => {
  it('needs a request and a reported level that differs', () => {
    expect(logLevelLagging(sidecar({ log_level: 'INFO', log_level_desired: 'DEBUG' }))).toBe(true);
    expect(logLevelLagging(sidecar({ log_level: 'DEBUG', log_level_desired: 'DEBUG' }))).toBe(false);
    expect(logLevelLagging(sidecar({ log_level: 'INFO', log_level_desired: null }))).toBe(false);
    expect(logLevelLagging(sidecar({ log_level_desired: 'DEBUG' }))).toBe(false);
  });
});

describe('loginDirsPending', () => {
  const report = { applied: { claude_config_dirs: ['/a'], codex_home: [] }, searched: {} } as unknown as Sidecar['login_dirs_report'];
  it('compares what the sidecar applied with what was asked for', () => {
    expect(loginDirsPending(sidecar())).toBe(false);
    expect(loginDirsPending(sidecar({ login_dirs_report: report, login_dirs_desired: { claude_config_dirs: ['/a'], codex_home: [] } }))).toBe(false);
    expect(loginDirsPending(sidecar({ login_dirs_report: report, login_dirs_desired: { claude_config_dirs: ['/b'], codex_home: [] } }))).toBe(true);
    expect(loginDirsPending(sidecar({ login_dirs_report: report }))).toBe(true);
  });
});

describe('pendingSettings', () => {
  it('counts every setting that is asked for but not applied', () => {
    expect(pendingSettings(sidecar(), fleet, false)).toBe(0);
    expect(pendingSettings(sidecar(), fleet, true)).toBe(1);
    expect(
      pendingSettings(sidecar({ auto_update: false, update_channel: 'edge', log_level: 'INFO', log_level_desired: 'DEBUG' }), fleet, true),
    ).toBe(4);
  });
});
