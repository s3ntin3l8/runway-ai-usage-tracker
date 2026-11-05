import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { sidecarLabel } from './queries';

interface PendingUsageSummaryProps {
  totalEvents: number;
  host: string;
  provider: string;
  search: string;
  matchingEvents: number;
  hasDefaultKeyedOption: boolean;
  selectedGroupCount: number;
  selectedEventCount: number;
  onAssignSelected: () => void;
  onClearSelection: () => void;
}

export function PendingUsageSummary({
  totalEvents,
  host,
  provider,
  search,
  matchingEvents,
  hasDefaultKeyedOption,
  selectedGroupCount,
  selectedEventCount,
  onAssignSelected,
  onClearSelection,
}: PendingUsageSummaryProps) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 className="text-sm font-semibold">Unassigned usage · {totalEvents} events</h2>
        {(host || provider || search) && (
          <p className="mt-1 text-xs text-fg-muted">
            {matchingEvents} events match the current filters.
          </p>
        )}
        <p className="mt-1 max-w-4xl text-xs text-fg-muted">
          These events are stored safely and excluded from account totals until assigned. Assigning maps future
          default-identity events for the same provider on the same host after its next config sync.
        </p>
        {hasDefaultKeyedOption && (
          <p className="mt-1 max-w-4xl text-xs text-fg-muted">
            An account labeled “(default)” remains under the shared default identity. Re-key it under Settings → Data health
            first to assign usage to its own account.
          </p>
        )}
      </div>
      {selectedGroupCount > 0 && (
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="text-fg-muted">
            {selectedGroupCount} groups · {selectedEventCount} events selected
          </span>
          <Button size="sm" variant="secondary" onClick={onAssignSelected}>
            Assign selected
          </Button>
          <Button size="sm" variant="ghost" onClick={onClearSelection}>
            Clear selection
          </Button>
        </div>
      )}
    </div>
  );
}

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

interface PendingUsagePaginationProps {
  offset: number;
  itemCount: number;
  totalGroups: number;
  matchingEvents: number;
  onPrevious: () => void;
  onNext: () => void;
}

export function PendingUsagePagination({
  offset,
  itemCount,
  totalGroups,
  matchingEvents,
  onPrevious,
  onNext,
}: PendingUsagePaginationProps) {
  return (
    <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-fg-muted">
      <span>
        {totalGroups === 0
          ? 'No matching groups'
          : `Showing ${offset + 1}–${Math.min(offset + itemCount, totalGroups)} of ${totalGroups} groups · ${matchingEvents} matching events`}
      </span>
      <div className="flex gap-2">
        <Button size="sm" disabled={offset === 0} onClick={onPrevious}>
          Previous
        </Button>
        <Button
          size="sm"
          disabled={offset + 100 >= totalGroups}
          onClick={onNext}
        >
          Next
        </Button>
      </div>
    </div>
  );
}
