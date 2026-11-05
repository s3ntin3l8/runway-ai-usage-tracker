import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { sidecarLabel } from './queries';

interface PendingUsageFiltersProps {
  host: string;
  provider: string;
  search: string;
  sidecars: string[];
  providers: string[];
  machineNames: Map<string, string>;
  totalGroups: number;
  selectingAll: boolean;
  onHostChange: (value: string) => void;
  onProviderChange: (value: string) => void;
  onSearchChange: (value: string) => void;
  onSelectAll: () => void;
}

export function PendingUsageFilters({
  host,
  provider,
  search,
  sidecars,
  providers,
  machineNames,
  totalGroups,
  selectingAll,
  onHostChange,
  onProviderChange,
  onSearchChange,
  onSelectAll,
}: PendingUsageFiltersProps) {
  return (
    <div className="mt-4 grid gap-2 sm:grid-cols-2 xl:grid-cols-[minmax(12rem,1fr)_minmax(12rem,1fr)_minmax(16rem,2fr)_auto]">
      <label className="text-xs font-medium text-fg-muted">
        Host
        <select
          aria-label="Filter unassigned usage by host"
          className="mt-1 h-9 w-full rounded-sm border border-edge bg-surface-2 px-2 text-[13px] text-fg"
          value={host}
          onChange={(event) => onHostChange(event.target.value)}
        >
          <option value="">All hosts</option>
          {sidecars.map((sidecar) => <option key={sidecar} value={sidecar}>{sidecarLabel(machineNames, sidecar)}</option>)}
        </select>
      </label>
      <label className="text-xs font-medium text-fg-muted">
        Provider
        <select
          aria-label="Filter unassigned usage by provider"
          className="mt-1 h-9 w-full rounded-sm border border-edge bg-surface-2 px-2 text-[13px] text-fg"
          value={provider}
          onChange={(event) => onProviderChange(event.target.value)}
        >
          <option value="">All providers</option>
          {providers.map((providerId) => <option key={providerId} value={providerId}>{providerId}</option>)}
        </select>
      </label>
      <label className="text-xs font-medium text-fg-muted">
        Search session or model
        <Input
          className="mt-1 h-9"
          aria-label="Search unassigned sessions and models"
          placeholder="Search…"
          value={search}
          onChange={(event) => onSearchChange(event.target.value)}
        />
      </label>
      {host && totalGroups > 0 && (
        <Button
          className="self-end"
          size="sm"
          variant="secondary"
          loading={selectingAll}
          disabled={selectingAll}
          onClick={() => void onSelectAll()}
        >
          Select all {totalGroups} matching groups
        </Button>
      )}
    </div>
  );
}
