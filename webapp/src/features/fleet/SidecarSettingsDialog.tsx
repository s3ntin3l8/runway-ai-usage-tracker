// Per-sidecar settings in three tabs: General (name, tags, login directories), Credentials (keep-alive and the
// Keychain sources) and Updates & logs. The Fleet card stays a summary; everything you can change lives here.
// A status line under the title says what the sidecar has not applied yet, once, for every section.

import { useState } from 'react';
import { SidecarSources } from './SidecarSources';
import { useSourcesNeedingAttention } from './sourceQueries';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { CircleArrowUp, TriangleAlert } from 'lucide-react';
import { toast } from 'sonner';
import { fetchAppConfig, patchSidecar, setSidecarKeepAlive, setSidecarSettings } from '@/api/endpoints';
import type { Sidecar, SidecarChannel, SidecarLogLevel } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Callout } from '@/components/ui/Callout';
import { Input, Label } from '@/components/ui/Input';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select';
import { Switch } from '@/components/ui/Switch';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs';
import {
  KEEP_ALIVE_PROVIDER_LABELS,
  type KeepAliveProvider,
  keepAliveLoginsText,
  keepAliveState,
  useKeepAliveFleetDefault,
} from '@/lib/keepAlive';
import { timeAgo } from '@/lib/format';
import {
  type FleetUpdateDefaults,
  logLevelLagging,
  loginDirsPending,
  pendingSettings,
  updateDrift,
} from './settingsSync';

/** What the sidecar runs now vs. what Runway asked for; shared by the card and the dialog. */
export function useSidecarKeepAlive(sidecar: Sidecar, online: boolean) {
  const fleetDefault = useKeepAliveFleetDefault();
  return keepAliveState({
    reported: sidecar.keep_alive ?? null,
    desired: sidecar.keep_alive_desired ?? null,
    offline: !online,
    fleetDefault,
  });
}

export function SidecarSettingsDialog({
  sidecar,
  online,
  onClose,
  onUpdate,
  onDelete,
}: {
  sidecar: Sidecar | null;
  online: boolean;
  onClose: () => void;
  onUpdate: (s: Sidecar) => void;
  onDelete: (s: Sidecar) => void;
}) {
  return (
    <ResponsiveDialog
      open={sidecar !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title="Sidecar settings"
      width="max-w-2xl"
      description={
        sidecar
          ? [sidecar.custom_name || sidecar.hostname || sidecar.sidecar_id, sidecar.os_platform, sidecar.sidecar_version && `v${sidecar.sidecar_version}`]
              .filter(Boolean)
              .join(' · ')
          : undefined
      }
    >
      {sidecar ? (
        <SettingsTabs
          sidecar={sidecar}
          online={online}
          onUpdate={() => onUpdate(sidecar)}
          onDelete={() => onDelete(sidecar)}
        />
      ) : null}
    </ResponsiveDialog>
  );
}

function SettingsTabs({
  sidecar,
  online,
  onUpdate,
  onDelete,
}: {
  sidecar: Sidecar;
  online: boolean;
  onUpdate: () => void;
  onDelete: () => void;
}) {
  const fleet = useFleetUpdateDefaults();
  const ka = useSidecarKeepAlive(sidecar, online);
  const pending = pendingSettings(sidecar, fleet, ka.pending);
  const drift = updateDrift(sidecar, fleet);
  // What on the Updates tab wants a look: a newer build, a drifted setting, a level that has not landed.
  const maintenance =
    (sidecar.update_available ? 1 : 0) + [drift.auto, drift.channel, logLevelLagging(sidecar)].filter(Boolean).length;
  const attention = useSourcesNeedingAttention(sidecar.sidecar_id, sidecar.os_platform);
  // A directory the sidecar has not applied yet lives on the General tab.
  const general = loginDirsPending(sidecar) ? 1 : 0;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-md bg-surface-2 px-3 py-2 text-xs text-fg-muted">
        <span aria-hidden className={`size-1.5 rounded-full ${online ? 'bg-ok' : 'bg-unknown'}`} />
        <span className="font-semibold text-fg">{online ? 'Online' : 'Offline'}</span>
        {sidecar.last_seen ? <span>last seen {timeAgo(sidecar.last_seen)}</span> : null}
        {sidecar.update_available ? <Badge variant="accent">update available</Badge> : null}
        {pending > 0 ? (
          <span className="ml-auto text-warning" data-testid="settings-pending">
            {pending} setting{pending === 1 ? '' : 's'}{' '}
            {online ? "waiting for the sidecar's next check-in" : 'will apply when it reconnects'}
          </span>
        ) : null}
      </div>
      <Tabs defaultValue="general">
        <TabsList>
          <TabsTrigger value="general">
            General{' '}
            {general > 0 ? <Badge variant="warning">{general}</Badge> : null}
          </TabsTrigger>
          <TabsTrigger value="credentials">
            Credentials{' '}
            {attention > 0 ? <Badge variant="warning">{attention}</Badge> : null}
          </TabsTrigger>
          <TabsTrigger value="maintenance">
            Updates &amp; logs{' '}
            {maintenance > 0 ? <Badge variant="warning">{maintenance}</Badge> : null}
          </TabsTrigger>
        </TabsList>
        <TabsContent value="general" className="flex flex-col gap-4">
          <GeneralForm key={sidecar.sidecar_id} sidecar={sidecar} />
          <LoginDirectoriesSection key={`${sidecar.sidecar_id}-dirs`} sidecar={sidecar} online={online} onUpdate={onUpdate} />
        </TabsContent>
        <TabsContent value="credentials" className="flex flex-col gap-4">
          <KeepAliveSection sidecar={sidecar} online={online} onUpdate={onUpdate} />
          <SidecarSources sidecarId={sidecar.sidecar_id} osPlatform={sidecar.os_platform} />
        </TabsContent>
        <TabsContent value="maintenance" className="flex flex-col gap-4">
          <UpdatesSection sidecar={sidecar} online={online} onUpdate={onUpdate} />
          <LoggingSection sidecar={sidecar} online={online} onUpdate={onUpdate} />
        </TabsContent>
      </Tabs>
      <section className="flex items-center justify-between gap-3 border-t border-edge pt-4">
        <p className="text-[12px] text-fg-muted">Removes the registry entry; collected usage stays.</p>
        <Button size="sm" variant="danger" onClick={onDelete}>
          Remove sidecar
        </Button>
      </section>
    </div>
  );
}

function GeneralForm({ sidecar }: { sidecar: Sidecar }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState(sidecar.custom_name ?? '');
  const [tags, setTags] = useState((sidecar.tags ?? []).join(', '));
  const parseTags = (text: string) =>
    text
      .split(',')
      .map((t) => t.trim())
      .filter(Boolean);

  const save = useMutation({
    mutationFn: () => patchSidecar(sidecar.sidecar_id, { custom_name: name.trim(), tags: parseTags(tags) }),
    onSuccess: () => {
      toast.success('Sidecar updated');
      queryClient.invalidateQueries({ queryKey: ['fleet', 'sidecars'] });
    },
    onError: (err) => toast.error(err.message),
  });
  // Saved on blur (or Enter) when something actually changed; there is no Save button.
  const commit = () => {
    const unchanged =
      name.trim() === (sidecar.custom_name ?? '') &&
      JSON.stringify(parseTags(tags)) === JSON.stringify(sidecar.tags ?? []);
    if (!unchanged && !save.isPending) save.mutate();
  };

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        commit();
      }}
      className="flex flex-col gap-3 rounded-lg border border-edge p-4"
    >
      <h3 className="text-[13px] font-semibold">Identity</h3>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="sidecar-name">Display name</Label>
        <Input
          id="sidecar-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onBlur={commit}
          placeholder={sidecar.hostname}
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="sidecar-tags">Tags (comma-separated)</Label>
        <Input
          id="sidecar-tags"
          value={tags}
          onChange={(e) => setTags(e.target.value)}
          onBlur={commit}
          placeholder="work, laptop"
        />
      </div>
      <p className="text-[11px] text-fg-subtle">Saved when you leave a field.</p>
    </form>
  );
}

function KeepAliveSection({
  sidecar,
  online,
  onUpdate,
}: {
  sidecar: Sidecar;
  online: boolean;
  onUpdate: () => void;
}) {
  const queryClient = useQueryClient();
  const ka = useSidecarKeepAlive(sidecar, online);
  const keepAliveWhen = online
    ? "applies on the sidecar's next check-in"
    : 'applies when the sidecar reconnects';
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['fleet', 'sidecars'] });

  const keepAlive = useMutation({
    mutationFn: (next: boolean) => setSidecarKeepAlive(sidecar.sidecar_id, next),
    onSuccess: (_data, next) => {
      toast.success(`Keep-alive ${next ? 'on' : 'off'} — ${keepAliveWhen}`);
      refresh();
    },
    onError: (err) => toast.error(err.message),
  });
  const clearKeepAlive = useMutation({
    mutationFn: () => setSidecarKeepAlive(sidecar.sidecar_id, null),
    onSuccess: () => {
      toast.success("Keep-alive override cleared — the sidecar's own setting applies");
      refresh();
    },
    onError: (err) => toast.error(err.message),
  });
  // One login's own override on top of the sidecar-level setting; null follows the sidecar again.
  const loginKeepAlive = useMutation({
    mutationFn: ({ provider, enabled }: { provider: KeepAliveProvider; enabled: boolean | null }) =>
      setSidecarKeepAlive(sidecar.sidecar_id, enabled, provider),
    onSuccess: (_data, { provider, enabled }) => {
      const name = KEEP_ALIVE_PROVIDER_LABELS[provider];
      toast.success(
        enabled === null
          ? `${name} follows the sidecar's keep-alive setting — ${keepAliveWhen}`
          : `${name} keep-alive ${enabled ? 'on' : 'off'} — ${keepAliveWhen}`,
      );
      refresh();
    },
    onError: (err) => toast.error(err.message),
  });

  // All of them PUT the same endpoint, so keep them single-flight together.
  const busy = keepAlive.isPending || clearKeepAlive.isPending || loginKeepAlive.isPending;
  // A sidecar that doesn't report per-login state can't apply per-login overrides.
  const loginsReported = sidecar.keep_alive_providers != null;
  const id = `keep-alive-${sidecar.sidecar_id}`;

  return (
    <section className="rounded-lg border border-edge p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <label htmlFor={id} className="text-[13px] font-semibold">
            Keep logins alive
          </label>
          <p className="mt-0.5 text-[11px] text-fg-subtle">
            Renews this sidecar&apos;s {keepAliveLoginsText()} logins itself so they don&apos;t
            lapse while the CLI is idle.
          </p>
        </div>
        <Switch
          id={id}
          checked={ka.effective}
          disabled={ka.unsupported || busy}
          onCheckedChange={(next) => keepAlive.mutate(next)}
          aria-describedby={`${id}-status`}
          data-pending={ka.pending ? 'true' : undefined}
        />
      </div>
      <p
        id={`${id}-status`}
        className={`mt-1.5 text-[11px] ${ka.pending ? 'text-warning' : 'text-fg-muted'}`}
      >
        {ka.status}
      </p>
      {ka.hasOverride ? (
        <p className="mt-1 text-[11px] text-fg-subtle">
          A Runway override is in charge of this sidecar.{' '}
          <button
            type="button"
            className="font-medium text-accent-text hover:underline disabled:opacity-50"
            disabled={busy}
            onClick={() => clearKeepAlive.mutate()}
          >
            Use the sidecar&apos;s own setting
          </button>
        </p>
      ) : null}

      {!ka.unsupported ? (
        <div className="mt-3 border-t border-edge pt-2.5">
          <p className="text-[11px] font-medium text-fg-muted">Per login</p>
          {!loginsReported ? (
            <p className="mt-1 text-[11px] text-fg-subtle">
              This sidecar (v{sidecar.sidecar_version ?? '?'}) doesn&apos;t report per-login
              keep-alive yet, so individual logins can&apos;t be set.{' '}
              {sidecar.update_available ? (
                <button
                  type="button"
                  className="font-medium text-accent-text hover:underline"
                  onClick={onUpdate}
                >
                  Update it
                </button>
              ) : (
                'Update it to a newer build to enable this.'
              )}
            </p>
          ) : (
            <div className="mt-1.5 flex flex-col gap-1.5">
              {(Object.keys(KEEP_ALIVE_PROVIDER_LABELS) as KeepAliveProvider[]).map((provider) => {
                const override = sidecar.keep_alive_desired_providers?.[provider];
                const running = sidecar.keep_alive_providers?.[provider] === true;
                const label = KEEP_ALIVE_PROVIDER_LABELS[provider];
                return (
                  <div key={provider} className="flex items-center justify-between gap-2">
                    <span className="min-w-0 truncate text-[12px]">
                      {label}
                      {running ? <span className="ml-1.5 text-success">running</span> : null}
                    </span>
                    <Select
                      value={override === true ? 'on' : override === false ? 'off' : 'default'}
                      disabled={busy}
                      onValueChange={(v) =>
                        loginKeepAlive.mutate({
                          provider,
                          enabled: v === 'default' ? null : v === 'on',
                        })
                      }
                    >
                      <SelectTrigger label={`${label} keep-alive`} className="h-8 w-44">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="default">
                          Follow sidecar ({ka.effective ? 'on' : 'off'})
                        </SelectItem>
                        <SelectItem value="on">Always on</SelectItem>
                        <SelectItem value="off">Always off</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      ) : null}
    </section>
  );
}

const CHANNELS: SidecarChannel[] = ['stable', 'beta', 'edge'];
const onOff = (v: boolean) => (v ? 'on' : 'off');
const sentence = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

/** The fleet-wide auto-update flag and channel from Settings → System (shares the cached query). */
function useFleetUpdateDefaults(): FleetUpdateDefaults {
  const { data } = useQuery({ queryKey: ['system', 'app-config'], queryFn: fetchAppConfig });
  return {
    autoUpdate: data?.sidecar_auto_update === true,
    channel: (data?.sidecar_update_channel as SidecarChannel | undefined) ?? 'stable',
  };
}

function UpdatesSection({
  sidecar,
  online,
  onUpdate,
}: {
  sidecar: Sidecar;
  online: boolean;
  onUpdate: () => void;
}) {
  const queryClient = useQueryClient();
  const fleet = useFleetUpdateDefaults();
  const when = online ? "applies on the sidecar's next check-in" : 'applies when the sidecar reconnects';

  const save = useMutation({
    mutationFn: (settings: Parameters<typeof setSidecarSettings>[1]) =>
      setSidecarSettings(sidecar.sidecar_id, settings),
    onSuccess: (_data, settings) => {
      const what = 'auto_update' in settings ? 'Auto-update' : 'Update channel';
      toast.success(`${what} saved — ${when}`);
      queryClient.invalidateQueries({ queryKey: ['fleet', 'sidecars'] });
    },
    onError: (err) => toast.error(err.message),
  });

  const drift = updateDrift(sidecar, fleet);
  const { effectiveAuto, effectiveChannel } = drift;
  const autoValue =
    sidecar.auto_update_desired == null ? 'default' : sidecar.auto_update_desired ? 'on' : 'off';
  const channelValue = sidecar.update_channel_desired ?? 'default';
  const canSelfUpdate = sidecar.self_update_capable !== false;
  // The sidecar says it uses something other than what Runway asks for: either the request has
  // not landed yet, or its own config.json / RUNWAY_UPDATE_CHANNEL wins over the dashboard.
  const autoDiffers = drift.auto;
  const channelDiffers = drift.channel;

  return (
    <section className="rounded-lg border border-edge p-4">
      <h3 className="text-[13px] font-semibold">Updates</h3>
      <p className="mt-0.5 text-[11px] text-fg-subtle">
        Overrides for this sidecar only; by default it follows Settings → System.
      </p>
      {sidecar.update_available ? (
        <Callout
          className="mt-3"
          icon={<CircleArrowUp className="size-4" />}
          action={
            <Button size="sm" variant="secondary" onClick={onUpdate}>
              Update now
            </Button>
          }
        >
          A newer sidecar build is available.
        </Callout>
      ) : null}
      <div className="mt-2 flex flex-col gap-1.5">
        <div className="flex items-center justify-between gap-2">
          <span className="text-[12px]">Auto-update</span>
          {canSelfUpdate ? (
            <Select
              value={autoValue}
              disabled={save.isPending}
              onValueChange={(v) =>
                save.mutate({ auto_update: v === 'default' ? null : v === 'on' })
              }
            >
              <SelectTrigger label="Auto-update" className="h-8 w-44">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="default">Follow fleet ({onOff(fleet.autoUpdate)})</SelectItem>
                <SelectItem value="on">Always on</SelectItem>
                <SelectItem value="off">Always off</SelectItem>
              </SelectContent>
            </Select>
          ) : (
            <span className="text-[11px] text-fg-subtle">Not supported by this build</span>
          )}
        </div>
        <div className="flex items-center justify-between gap-2">
          <span className="text-[12px]">Update channel</span>
          <Select
            value={channelValue}
            disabled={save.isPending}
            onValueChange={(v) =>
              save.mutate({ update_channel: v === 'default' ? null : (v as SidecarChannel) })
            }
          >
            <SelectTrigger label="Update channel" className="h-8 w-44">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="default">Follow fleet ({fleet.channel})</SelectItem>
              {CHANNELS.map((c) => (
                <SelectItem key={c} value={c}>
                  {c[0].toUpperCase() + c.slice(1)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>
      {!canSelfUpdate ? (
        <p className="mt-1.5 text-[11px] text-fg-subtle">
          This build (from source or Docker) can&apos;t replace itself, so it is never updated
          automatically.
        </p>
      ) : null}
      {canSelfUpdate && (autoDiffers || channelDiffers) ? (
        <div className="mt-3" data-testid="update-differs">
          <Callout tone="warning" icon={<TriangleAlert className="size-4" />}>
            {sentence(
              [
                autoDiffers
                  ? `reports auto-update ${onOff(sidecar.auto_update === true)} (Runway asks for ${onOff(effectiveAuto)})`
                  : null,
                channelDiffers
                  ? `reports the ${sidecar.update_channel} channel (Runway asks for ${effectiveChannel})`
                  : null,
              ]
                .filter(Boolean)
                .join('; '),
            )}
            . Either the change {when}, or this sidecar&apos;s own config.json or RUNWAY_UPDATE_CHANNEL overrides the
            dashboard.
          </Callout>
        </div>
      ) : null}
    </section>
  );
}

const LOG_LEVELS: SidecarLogLevel[] = ['DEBUG', 'INFO', 'WARNING', 'ERROR'];

function LoggingSection({
  sidecar,
  online,
  onUpdate,
}: {
  sidecar: Sidecar;
  online: boolean;
  onUpdate: () => void;
}) {
  const queryClient = useQueryClient();
  const when = online ? "applies on the sidecar's next check-in" : 'applies when the sidecar reconnects';
  const save = useMutation({
    mutationFn: (level: SidecarLogLevel | null) =>
      setSidecarSettings(sidecar.sidecar_id, { log_level: level }),
    onSuccess: (_data, level) => {
      toast.success(
        level ? `Log level ${level} saved — ${when}` : `Log level back to the sidecar's own — ${when}`,
      );
      queryClient.invalidateQueries({ queryKey: ['fleet', 'sidecars'] });
    },
    onError: (err) => toast.error(err.message),
  });

  // A sidecar that doesn't report its level predates the remote setting and would ignore it.
  const supported = sidecar.log_level != null;
  const desired = sidecar.log_level_desired ?? null;
  const raised = desired === 'DEBUG';
  const lagging = logLevelLagging(sidecar);

  return (
    <section className="rounded-lg border border-edge p-4">
      <h3 className="text-[13px] font-semibold">Logging</h3>
      <p className="mt-0.5 text-[11px] text-fg-subtle">
        Raise the level while debugging; read the output with Logs on the card. The sidecar&apos;s
        own level applies again when you reset it.
      </p>
      {!supported ? (
        <p className="mt-1.5 text-[11px] text-fg-subtle">
          This sidecar (v{sidecar.sidecar_version ?? '?'}) doesn&apos;t report its log level yet, so
          it can&apos;t be changed from here.{' '}
          {sidecar.update_available ? (
            <button
              type="button"
              className="font-medium text-accent-text hover:underline"
              onClick={onUpdate}
            >
              Update it
            </button>
          ) : (
            'Update it to a newer build to enable this.'
          )}
        </p>
      ) : (
        <>
          <div className="mt-2 flex items-center justify-between gap-2">
            <span className="text-[12px]">
              Log level
              <span className="ml-1.5 text-fg-subtle">now {sidecar.log_level}</span>
            </span>
            <Select
              value={desired ?? 'default'}
              disabled={save.isPending}
              onValueChange={(v) => save.mutate(v === 'default' ? null : (v as SidecarLogLevel))}
            >
              <SelectTrigger label="Log level" className="h-8 w-44">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="default">Use sidecar config</SelectItem>
                {LOG_LEVELS.map((l) => (
                  <SelectItem key={l} value={l}>
                    {l[0] + l.slice(1).toLowerCase()}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          {lagging ? (
            <div className="mt-3" data-testid="log-level-pending">
              <Callout tone="warning" icon={<TriangleAlert className="size-4" />}>
                Requested {desired}; the sidecar still logs at {sidecar.log_level}. It {when}.
              </Callout>
            </div>
          ) : null}
          {raised ? (
            <p className="mt-1.5 text-[11px] text-fg-muted">
              Debug logging is verbose and stays on until you reset it.{' '}
              <button
                type="button"
                className="font-medium text-accent-text hover:underline disabled:opacity-50"
                disabled={save.isPending}
                onClick={() => save.mutate(null)}
              >
                Reset to the sidecar&apos;s own level
              </button>
            </p>
          ) : null}
        </>
      )}
    </section>
  );
}

function LoginDirectoriesSection({ sidecar, online, onUpdate }: {
  sidecar: Sidecar; online: boolean; onUpdate: () => void;
}) {
  const queryClient = useQueryClient();
  const desired = sidecar.login_dirs_desired ?? { claude_config_dirs: [], codex_home: [] };
  const [claude, setClaude] = useState(desired.claude_config_dirs.join('\n'));
  const [codex, setCodex] = useState(desired.codex_home.join('\n'));
  const split = (text: string) => [...new Set(text.split('\n').map(p => p.trim()).filter(Boolean))];
  const save = useMutation({
    mutationFn: (clear: boolean) => setSidecarSettings(sidecar.sidecar_id, {
      claude_config_dirs: clear ? null : split(claude),
      codex_home: clear ? null : split(codex),
    }),
    onSuccess: (_data, clear) => {
      if (clear) { setClaude(''); setCodex(''); }
      toast.success('Login directories saved — apply on next check-in');
      queryClient.invalidateQueries({ queryKey: ['fleet', 'sidecars'] });
    },
    onError: err => toast.error(err.message),
  });
  const report = sidecar.login_dirs_report;
  const pending = loginDirsPending(sidecar);
  return (
    <section className="rounded-lg border border-edge p-4">
      <h3 className="text-[13px] font-semibold">Login directories</h3>
      <p className="mt-1 text-[11px] text-fg-subtle">
        Extra Claude Code and Codex directories on this machine, one per line. Standard locations,
        environment variables and the sidecar's local configuration are also searched. Use directories,
        not credential files; ~ refers to the user running the sidecar.
      </p>
      {!report ? (
        <p className="mt-2 text-[11px] text-fg-muted">
          This sidecar does not report login directories yet. Update it to a newer build to configure them.
          {sidecar.update_available ? <button type="button" className="ml-1 text-accent-text underline" onClick={onUpdate}>Update it</button> : null}
        </p>
      ) : (
        <form className="mt-3 flex flex-col gap-2" onSubmit={e => { e.preventDefault(); save.mutate(false); }}>
          {([['claude', 'Claude Code directories', claude, setClaude], ['codex', 'Codex directories', codex, setCodex]] as const).map(([key, label, value, update]) => (
            // Most devices add none, so the field stays folded until it has content or is opened.
            <details key={key} open={value.trim() !== '' || undefined} className="text-xs">
              <summary className="cursor-pointer text-accent-text">
                {label} · {split(value).length ? `${split(value).length} added` : 'none added'}
              </summary>
              <div className="mt-1.5 flex flex-col gap-1">
                <Label htmlFor={`${sidecar.sidecar_id}-${key}-dirs`}>{label}</Label>
                <textarea id={`${sidecar.sidecar_id}-${key}-dirs`} rows={3} value={value} onChange={e => update(e.target.value)} disabled={save.isPending}
                  className="w-full rounded-sm border border-edge bg-surface-1 p-2 text-sm text-fg focus:border-accent" />
              </div>
            </details>
          ))}
          <p className="text-[11px] text-fg-muted" role="status">
            {!online ? 'Sidecar is offline — applies when it reconnects.' : pending ? 'Requested directories — waiting for the sidecar to confirm.' : 'Fleet directories confirmed by the sidecar.'}
          </p>
          <div className="flex gap-2">
            <Button size="sm" type="submit" loading={save.isPending}>Save directories</Button>
            <Button size="sm" type="button" disabled={save.isPending} onClick={() => save.mutate(true)}>Clear Fleet additions</Button>
          </div>
        </form>
      )}
    </section>
  );
}
