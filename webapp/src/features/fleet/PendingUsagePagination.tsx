import { Button } from '@/components/ui/Button';

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
