// Per-sidecar settings in three tabs: General (name, tags, login directories), Credentials (keep-alive and the
// Keychain sources) and Updates & logs. The Fleet card stays a summary; everything you can change lives here.
// A status line under the title says what the sidecar has not applied yet, once, for every section.

import type { Sidecar } from '@/api/types';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';
import { SettingsTabs } from './SettingsTabs';

export { useSidecarKeepAlive } from './useSidecarKeepAlive';

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
