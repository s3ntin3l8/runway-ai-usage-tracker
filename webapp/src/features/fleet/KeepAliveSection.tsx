import { useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { setSidecarKeepAlive } from '@/api/endpoints';
import type { Sidecar } from '@/api/types';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select';
import { Switch } from '@/components/ui/Switch';
import {
  KEEP_ALIVE_PROVIDER_LABELS,
  type KeepAliveProvider,
  keepAliveLoginsText,
} from '@/lib/keepAlive';
import { useSidecarKeepAlive } from './useSidecarKeepAlive';

export function KeepAliveSection({
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
