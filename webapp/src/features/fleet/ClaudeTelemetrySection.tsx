import { useMutation, useQueryClient } from '@tanstack/react-query';
import { TriangleAlert } from 'lucide-react';
import { toast } from 'sonner';
import { setSidecarSettings } from '@/api/endpoints';
import type { Sidecar } from '@/api/types';
import { Callout } from '@/components/ui/Callout';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select';
import { claudeTelemetryPending } from './settingsSync';
import { onOff } from './settingsText';
import { useFleetUpdateDefaults } from './useFleetUpdateDefaults';

export function ClaudeTelemetrySection({
  sidecar,
  online,
}: {
  sidecar: Sidecar;
  online: boolean;
}) {
  const queryClient = useQueryClient();
  const fleet = useFleetUpdateDefaults();
  const when = online ? "applies on the sidecar's next check-in" : 'applies when the sidecar reconnects';

  const save = useMutation({
    mutationFn: (claude_telemetry: boolean | null) =>
      setSidecarSettings(sidecar.sidecar_id, { claude_telemetry }),
    onSuccess: () => {
      toast.success(`Claude Code telemetry saved — ${when}`);
      queryClient.invalidateQueries({ queryKey: ['fleet', 'sidecars'] });
    },
    onError: (err) => toast.error(err.message),
  });

  const value =
    sidecar.claude_telemetry_desired == null
      ? 'default'
      : sidecar.claude_telemetry_desired
        ? 'on'
        : 'off';
  const fleetDefault = fleet.claudeTelemetry ?? false;
  const pending = claudeTelemetryPending(sidecar, fleet);
  const status = sidecar.claude_telemetry;

  return (
    <section className="rounded-lg border border-edge p-4">
      <h3 className="text-[13px] font-semibold">Claude Code telemetry</h3>
      <p className="mt-0.5 text-[11px] text-fg-subtle">
        Captures Claude Code side calls (summaries, compaction, prompt suggestions, hook prompts,
        session titles) that local transcripts omit. Turning this on writes OpenTelemetry keys into{' '}
        <code>~/.claude/settings.json</code> for the sidecar&apos;s local receiver; only sessions
        started afterwards are covered.
      </p>
      <div className="mt-2 flex items-center justify-between gap-2">
        <span className="text-[12px]">
          OpenTelemetry export
          {status != null ? (
            <span className="ml-1.5 text-fg-subtle">now {status}</span>
          ) : null}
        </span>
        <Select
          value={value}
          disabled={save.isPending}
          onValueChange={(v) =>
            save.mutate(v === 'default' ? null : v === 'on')
          }
        >
          <SelectTrigger label="Claude Code telemetry" className="h-8 w-44">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="default">Follow fleet ({onOff(fleetDefault)})</SelectItem>
            <SelectItem value="on">Always on</SelectItem>
            <SelectItem value="off">Always off</SelectItem>
          </SelectContent>
        </Select>
      </div>
      {status === 'conflict' ? (
        <div className="mt-3" data-testid="claude-telemetry-conflict">
          <Callout tone="warning" icon={<TriangleAlert className="size-4" />}>
            Conflict: <code>~/.claude/settings.json</code> already sets a different OpenTelemetry
            value, so Runway left the file untouched.
          </Callout>
        </div>
      ) : null}
      {status === 'error' ? (
        <div className="mt-3" data-testid="claude-telemetry-error">
          <Callout tone="warning" icon={<TriangleAlert className="size-4" />}>
            Could not update <code>~/.claude/settings.json</code> (unparseable JSON or concurrent
            edit). The file was left untouched.
          </Callout>
        </div>
      ) : null}
      {pending ? (
        <div className="mt-3" data-testid="claude-telemetry-pending">
          <Callout tone="warning" icon={<TriangleAlert className="size-4" />}>
            Requested {onOff(sidecar.effective_claude_telemetry ?? fleetDefault)}; the sidecar
            currently reports {status}. It {when}.
          </Callout>
        </div>
      ) : null}
    </section>
  );
}
