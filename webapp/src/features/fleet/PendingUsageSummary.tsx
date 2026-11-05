import { Button } from '@/components/ui/Button';

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
