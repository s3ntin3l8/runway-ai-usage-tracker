import { useState } from 'react';
import { Check } from 'lucide-react';
import { useProjectFilter } from '@/hooks/useProjectFilter';
import { useProjects, type DateRange } from '@/features/provider/queries';
import { cn } from '@/lib/cn';
import { Input } from './Input';

// A searchable single-select: the project list holds hundreds of entries, so a
// plain select is unusable. Type to narrow, click to pick; "All projects" clears.
export function ProjectFilter({ providerId, range }: { providerId: string; range?: DateRange }) {
  const [project, setProject] = useProjectFilter();
  const [query, setQuery] = useState('');
  const projects = useProjects(providerId, range);
  const names = new Set(projects.data?.projects ?? []);
  // Keep a URL-selected project visible even when the window no longer lists it.
  if (project) names.add(project);
  const needle = query.trim().toLowerCase();
  const matches = [...names].filter((name) => name.toLowerCase().includes(needle));

  const options = [
    ...(needle ? [] : [{ value: undefined, label: 'All projects' }]),
    ...matches.map((name) => ({ value: name, label: name })),
  ];

  return (
    <div className="space-y-1">
      <Input
        type="search"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        placeholder="Search projects"
        aria-label="Search projects"
        className="h-8"
      />
      <div role="listbox" aria-label="Project filter" className="max-h-48 overflow-y-auto rounded-sm border border-edge">
        {options.map((option) => {
          const selected = option.value === project;
          return (
            <button
              key={option.value ?? '__all__'}
              type="button"
              role="option"
              aria-selected={selected}
              onClick={() => setProject(option.value)}
              className={cn(
                'flex h-8 w-full cursor-pointer items-center justify-between gap-2 px-2 text-left text-[13px] text-fg outline-none',
                'hover:bg-surface-2 focus-visible:bg-surface-2',
                selected && 'bg-surface-2',
              )}
            >
              <span className="truncate">{option.label}</span>
              {selected ? <Check className="size-3.5 shrink-0 text-accent-text" aria-hidden /> : null}
            </button>
          );
        })}
        {options.length === 0 ? <p className="px-2 py-2 text-[11px] text-fg-subtle">No matching projects</p> : null}
      </div>
    </div>
  );
}
