import { useState } from 'react';
import { Check } from 'lucide-react';
import { useProjectFilter } from '@/hooks/useProjectFilter';
import { NO_PROJECT } from '@/hooks/useSessionFilters';
import { useProjects, type DateRange } from '@/features/provider/queries';
import { cn } from '@/lib/cn';
import { Input } from './Input';

// A searchable single-select: the project list holds hundreds of entries, so a
// plain select is unusable. Type to narrow, click to pick; "All projects" clears,
// and "No project" selects sessions whose events carry no project label.
// Plain pressed-state buttons (not a listbox): the options are ordinary tab stops.
export function ProjectFilter({ providerId, range }: { providerId: string; range?: DateRange }) {
  const [project, setProject] = useProjectFilter();
  const [query, setQuery] = useState('');
  const projects = useProjects(providerId, range);
  const names = new Set(projects.data?.projects ?? []);
  // Keep a URL-selected project visible even when the window no longer lists it.
  if (project && project !== NO_PROJECT) names.add(project);
  const needle = query.trim().toLowerCase();
  const matches = [...names].filter((name) => name.toLowerCase().includes(needle));

  // Nothing to filter by (provider without project labels): hide the section —
  // unless "No project" is already selected, which must stay clearable.
  if (projects.isSuccess && names.size === 0 && project !== NO_PROJECT) return null;

  const pick = (value: string | undefined, label: string) => {
    const selected = value === project;
    return (
      <button
        key={value ?? '__all__'}
        type="button"
        aria-pressed={selected}
        onClick={() => setProject(value)}
        className={cn(
          'flex h-8 w-full cursor-pointer items-center justify-between gap-2 px-2 text-left text-[13px] text-fg outline-none',
          'hover:bg-surface-2 focus-visible:bg-surface-2',
          selected && 'bg-surface-2',
        )}
      >
        <span className="truncate">{label}</span>
        {selected ? <Check className="size-3.5 shrink-0 text-accent-text" aria-hidden /> : null}
      </button>
    );
  };

  return (
    <div role="group" aria-label="Project filter" className="space-y-1">
      <div className="text-[11px] text-fg-muted">Project</div>
      <Input
        type="search"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        placeholder="Search projects"
        aria-label="Search projects"
        className="h-8"
      />
      {/* Pinned above the scrolling list so a project can be cleared mid-search. */}
      <div className="divide-y divide-edge rounded-sm border border-edge">
        {pick(undefined, 'All projects')}
        {pick(NO_PROJECT, 'No project')}
      </div>
      <div className="max-h-48 overflow-y-auto rounded-sm border border-edge">
        {matches.map((name) => pick(name, name))}
        {matches.length === 0 ? (
          <p className="px-2 py-2 text-[11px] text-fg-subtle">
            {projects.isPending ? 'Loading projects…' : projects.isError ? 'Projects unavailable' : 'No matching projects'}
          </p>
        ) : null}
      </div>
    </div>
  );
}
