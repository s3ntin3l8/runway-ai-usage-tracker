// Header of the provider detail dialog: the Claude connection picker (when
// the card groups a family) and the master "all accounts enabled" toggle.

import type { ProviderAccount, ProviderConfig } from '@/api/types';
import { ProviderGlyph } from '@/components/ui/ProviderGlyph';
import { Switch } from '@/components/ui/Switch';
import { claudeConnectionLabel } from '@/lib/providerFamilies';

interface ProviderDetailHeaderProps {
  provider: ProviderConfig;
  connections?: ProviderConfig[];
  onSelectConnection?: (providerId: string) => void;
  masterToggleScopeId: string;
  masterEnabled: boolean;
  addressableConfigAccounts: ProviderAccount[];
  setMasterEnabled: {
    isPending: boolean;
    mutate: (next: boolean) => void;
  };
}

export function ProviderDetailHeader({
  provider,
  connections,
  onSelectConnection,
  masterToggleScopeId,
  masterEnabled,
  addressableConfigAccounts,
  setMasterEnabled,
}: ProviderDetailHeaderProps) {
  return (
    <>
      {connections && connections.length > 1 ? (
        <label className="flex flex-col gap-1.5 text-sm">
          Claude connection
          <select aria-label="Claude connection" value={provider.provider_id}
            className="rounded-md border border-edge bg-surface-1 p-2 text-fg"
            onChange={(e) => onSelectConnection?.(e.target.value)}>
            {connections.map((p) => <option key={p.provider_id} value={p.provider_id}>{claudeConnectionLabel(p.provider_id)}</option>)}
          </select>
        </label>
      ) : null}
      <div className="flex items-center justify-between rounded-sm border border-edge bg-surface-2 px-3 py-2">
        <div className="flex items-center gap-2">
          <ProviderGlyph providerId={provider.provider_id} name={provider.name} />
          <div>
            <p className="text-[13px] font-medium">All accounts enabled</p>
            {/* The scope of the switch lives here. It is wired to the
                switch with aria-describedby so a screen reader gets the
                qualifier with the name, not only on screen. */}
            <p id={masterToggleScopeId} className="text-[11px] text-fg-subtle">
              Turning this off hides the provider from collection for every account this
              card can address. An account whose provider is unavailable is left as it is.
            </p>
          </div>
        </div>
        <Switch
          checked={masterEnabled}
          disabled={addressableConfigAccounts.length === 0 || setMasterEnabled.isPending}
          onCheckedChange={(v) => setMasterEnabled.mutate(v)}
          aria-label="All accounts enabled"
          aria-describedby={masterToggleScopeId}
        />
      </div>
    </>
  );
}
