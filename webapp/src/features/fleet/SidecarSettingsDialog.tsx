// Per-sidecar settings: name/tags, keep-alive (sidecar-wide and per login), update and removal.
// The Fleet card stays a summary; everything you can change lives here.

import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { ArrowUpCircle } from 'lucide-react';
import { toast } from 'sonner';
import { patchSidecar, setSidecarKeepAlive } from '@/api/endpoints';
import type { Sidecar } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { Input, Label } from '@/components/ui/Input';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select';
import { Switch } from '@/components/ui/Switch';
import {
  KEEP_ALIVE_PROVIDER_LABELS,
  type KeepAliveProvider,
  keepAliveLoginsText,
  keepAliveState,
  useKeepAliveFleetDefault,
} from '@/lib/keepAlive';

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
  const queryClient = useQueryClient();
  return (
    <ResponsiveDialog
      open={sidecar !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title="Sidecar settings"
      description={
        sidecar
          ? [sidecar.custom_name || sidecar.hostname || sidecar.sidecar_id, sidecar.os_platform, sidecar.sidecar_version && `v${sidecar.sidecar_version}`]
              .filter(Boolean)
              .join(' · ')
          : undefined
      }
    >
      {sidecar ? (
        <div className="flex flex-col gap-5">
          <GeneralForm
            key={sidecar.sidecar_id}
            sidecar={sidecar}
            onSaved={() => {
              queryClient.invalidateQueries({ queryKey: ['fleet', 'sidecars'] });
              onClose();
            }}
          />
          <KeepAliveSection sidecar={sidecar} online={online} onUpdate={() => onUpdate(sidecar)} />
          {sidecar.update_available ? (
            <section className="flex items-center justify-between gap-3 border-t border-edge pt-4">
              <p className="text-[12px] text-fg-muted">A newer sidecar build is available.</p>
              <Button size="sm" variant="secondary" onClick={() => onUpdate(sidecar)}>
                <ArrowUpCircle className="size-3.5" aria-hidden />
                Update now
              </Button>
            </section>
          ) : null}
          <section className="flex items-center justify-between gap-3 border-t border-edge pt-4">
            <p className="text-[12px] text-fg-muted">
              Removes the registry entry; collected usage stays.
            </p>
            <Button size="sm" variant="danger" onClick={() => onDelete(sidecar)}>
              Remove sidecar
            </Button>
          </section>
        </div>
      ) : null}
    </ResponsiveDialog>
  );
}

function GeneralForm({ sidecar, onSaved }: { sidecar: Sidecar; onSaved: () => void }) {
  const [name, setName] = useState(sidecar.custom_name ?? '');
  const [tags, setTags] = useState((sidecar.tags ?? []).join(', '));

  const save = useMutation({
    mutationFn: () =>
      patchSidecar(sidecar.sidecar_id, {
        custom_name: name.trim(),
        tags: tags
          .split(',')
          .map((t) => t.trim())
          .filter(Boolean),
      }),
    onSuccess: () => {
      toast.success('Sidecar updated');
      onSaved();
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
      className="flex flex-col gap-3"
    >
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="sidecar-name">Display name</Label>
        <Input
          id="sidecar-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder={sidecar.hostname}
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="sidecar-tags">Tags (comma-separated)</Label>
        <Input
          id="sidecar-tags"
          value={tags}
          onChange={(e) => setTags(e.target.value)}
          placeholder="work, laptop"
        />
      </div>
      <Button type="submit" variant="primary" className="mt-1" loading={save.isPending}>
        Save
      </Button>
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
    <section className="rounded-md border border-edge bg-surface-2 p-2.5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <label htmlFor={id} className="text-[12px] font-medium">
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
            className="font-medium text-accent hover:underline disabled:opacity-50"
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
                  className="font-medium text-accent hover:underline"
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
                      <SelectTrigger aria-label={`${label} keep-alive`} className="h-8 w-44">
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
