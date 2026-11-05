// Disclosure caret and the filter tiles above the source list.

import { ChevronDown, ChevronRight } from 'lucide-react';
import { cn } from '@/lib/cn';

export function Caret({ open }: { open: boolean }) {
  const Icon = open ? ChevronDown : ChevronRight;
  return <Icon className="size-3.5 text-fg-subtle" aria-hidden />;
}

export function Tile({
  value,
  label,
  active,
  tone,
  onClick,
}: {
  value: string | number;
  label: string;
  active: boolean;
  tone?: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      aria-label={`${value} ${label}`}
      onClick={onClick}
      className={cn(
        'flex min-w-0 cursor-pointer flex-col rounded-md border bg-surface-2 px-2.5 py-1.5 text-left transition-colors hover:bg-surface-3',
        active ? 'border-accent ring-1 ring-accent' : 'border-edge',
      )}
    >
      <span className={cn('text-[15px] font-semibold', tone)}>{value}</span>
      <span className="text-[11px] text-fg-muted">{label}</span>
    </button>
  );
}
