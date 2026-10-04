// The credential rows under one account or machine: a column header, the rows that matter,
// and the dead ones folded away behind a toggle so a long-expired login doesn't push the
// healthy ones down the page.

import { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import type { CredentialSourceView } from '@/api/types';
import { isInactive } from './display';
import { SourceRow, sourceGrid } from './SourceRow';

interface SourceListProps {
  sources: CredentialSourceView[];
  label: string;
  /** Provider/account shown under each row's name (the By-machine view). */
  contextFor?: (source: CredentialSourceView) => string;
  /** The list is already scoped to one machine, so the Where column is redundant. */
  showMachine?: boolean;
}

export function SourceList({ sources, label, contextFor, showMachine = true }: SourceListProps) {
  const [expanded, setExpanded] = useState(false);
  const inactive = sources.filter(isInactive);
  const healthy = sources.filter((s) => !isInactive(s));
  // Only fold when something healthy remains; a list that is all dead shows all of it.
  const folded = healthy.length > 0 && inactive.length > 0 && !expanded;
  const visible = folded ? healthy : sources;
  const grid = sourceGrid(showMachine);

  return (
    <div className="mt-1">
      <div
        aria-hidden
        className={`hidden gap-x-3 border-b border-edge pb-1 text-[10px] font-medium uppercase tracking-wide text-fg-subtle md:grid ${grid}`}
      >
        <span>Status</span>
        <span>Credential</span>
        {showMachine ? <span>Machine</span> : null}
        <span>Expires</span>
        <span>Last collected</span>
        <span />
      </div>
      <ul className="divide-y divide-edge" aria-label={label}>
        {visible.map((s) => (
          <SourceRow
            key={`${s.provider_id}/${s.account_id}/${s.source_id}`}
            source={s}
            context={contextFor?.(s)}
            showMachine={showMachine}
          />
        ))}
      </ul>
      {healthy.length > 0 && inactive.length > 0 ? (
        <button
          type="button"
          aria-expanded={expanded}
          onClick={() => setExpanded((v) => !v)}
          className="mt-1 flex items-center gap-1 rounded-sm py-1 text-[11px] text-fg-muted hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          {expanded ? (
            <ChevronDown className="size-3" aria-hidden />
          ) : (
            <ChevronRight className="size-3" aria-hidden />
          )}
          {expanded
            ? 'Hide inactive credentials'
            : `Show ${inactive.length} inactive ${inactive.length === 1 ? 'credential' : 'credentials'}`}
        </button>
      ) : null}
    </div>
  );
}
