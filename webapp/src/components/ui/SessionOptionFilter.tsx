import { useEffect, useState } from 'react';
import { Check } from 'lucide-react';
import { useBranchFilter, useModelFilter } from '@/hooks/useSessionFilters';
import { useSessionFilterOptions, type DateRange } from '@/features/provider/queries';
import { cn } from '@/lib/cn';
import { Input } from './Input';

const SEARCH_DEBOUNCE_MS = 250;

const COPY = {
  model: { title: 'Model', all: 'All models', placeholder: 'Search models', none: 'No matching models' },
  branch: { title: 'Branch', all: 'All branches', placeholder: 'Search branches', none: 'No matching branches' },
} as const;

// Searchable single-select for the Sessions model / branch filters. The server
// returns only the top entries by usage, so typing searches server-side
// (debounced) rather than only narrowing the capped list.
export function SessionOptionFilter({
  kind,
  providerId,
  accountId,
  range,
}: {
  kind: 'model' | 'branch';
  providerId: string;
  accountId: string;
  range?: DateRange;
}) {
  const copy = COPY[kind];
  const useFilter = kind === 'model' ? useModelFilter : useBranchFilter;
  const [selected, setSelected] = useFilter();
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState('');
  useEffect(() => {
    const timer = setTimeout(() => setSearch(query.trim()), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [query]);
  const options = useSessionFilterOptions(kind, providerId, accountId, range, search);
  const names = new Set(options.data ?? []);
  // Keep a URL-selected value visible even when the list no longer carries it.
  if (selected && !search) names.add(selected);
  const matches = [...names];

  // Nothing to filter by (no models / branches recorded): hide the section.
  if (options.isSuccess && !search && names.size === 0) return null;

  const pick = (value: string | undefined, label: string) => {
    const isSelected = value === selected;
    return (
      <button
        key={value ?? '__all__'}
        type="button"
        aria-pressed={isSelected}
        onClick={() => setSelected(value)}
        className={cn(
          'flex h-8 w-full cursor-pointer items-center justify-between gap-2 px-2 text-left text-[13px] text-fg outline-none',
          'hover:bg-surface-2 focus-visible:bg-surface-2',
          isSelected && 'bg-surface-2',
        )}
      >
        <span className="truncate">{label}</span>
        {isSelected ? <Check className="size-3.5 shrink-0 text-accent-text" aria-hidden /> : null}
      </button>
    );
  };

  return (
    <div role="group" aria-label={`${copy.title} filter`} className="space-y-1">
      <div className="text-[11px] text-fg-muted">{copy.title}</div>
      <Input
        type="search"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        placeholder={copy.placeholder}
        aria-label={copy.placeholder}
        className="h-8"
      />
      <div className="rounded-sm border border-edge">{pick(undefined, copy.all)}</div>
      <div className="max-h-40 overflow-y-auto rounded-sm border border-edge">
        {matches.map((name) => pick(name, name))}
        {matches.length === 0 ? (
          <p className="px-2 py-2 text-[11px] text-fg-subtle">
            {options.isPending ? 'Loading…' : options.isError ? 'Unavailable' : copy.none}
          </p>
        ) : null}
      </div>
    </div>
  );
}
