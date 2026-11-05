import { useMutation, useQueryClient } from '@tanstack/react-query';
import { CircleArrowUp, TriangleAlert } from 'lucide-react';
import { toast } from 'sonner';
import { setSidecarSettings } from '@/api/endpoints';
import type { Sidecar, SidecarChannel } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { Callout } from '@/components/ui/Callout';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select';
import { updateDrift } from './settingsSync';
import { onOff, sentence } from './settingsText';
import { useFleetUpdateDefaults } from './useFleetUpdateDefaults';

const CHANNELS: SidecarChannel[] = ['stable', 'beta', 'edge'];

export function UpdatesSection({
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
