import type { Sidecar } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs';
import { timeAgo } from '@/lib/format';
import { GeneralForm } from './GeneralForm';
import { KeepAliveSection } from './KeepAliveSection';
import { LoggingSection } from './LoggingSection';
import { LoginDirectoriesSection } from './LoginDirectoriesSection';
import { SidecarSources } from './SidecarSources';
import { UpdatesSection } from './UpdatesSection';
import {
  logLevelLagging,
  loginDirsPending,
  pendingSettings,
  updateDrift,
} from './settingsSync';
import { useSourcesNeedingAttention } from './sourceQueries';
import { useFleetUpdateDefaults } from './useFleetUpdateDefaults';
import { useSidecarKeepAlive } from './useSidecarKeepAlive';

export function SettingsTabs({
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
