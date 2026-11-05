import { useMutation, useQueryClient } from '@tanstack/react-query';
import { TriangleAlert } from 'lucide-react';
import { toast } from 'sonner';
import { setSidecarSettings } from '@/api/endpoints';
import type { Sidecar, SidecarLogLevel } from '@/api/types';
import { Callout } from '@/components/ui/Callout';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select';
import { logLevelLagging } from './settingsSync';

const LOG_LEVELS: SidecarLogLevel[] = ['DEBUG', 'INFO', 'WARNING', 'ERROR'];

export function LoggingSection({
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
