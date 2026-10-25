// A mutually exclusive value picker — a "segmented control".
//
// It is NOT a tab list. These controls switch what a single panel shows (a
// chart's metric, a list's unit); they do not reveal a tab panel. Rendering
// them as Radix tabs left every trigger pointing at a tabpanel that did not
// exist — `aria-controls` dangling on an element that is not in the document
// — and a screen reader announced a "tab" with no panel to jump to.
//
// Native radio inputs give the correct semantics for free: the group is
// announced as a group, the chosen option as "selected", and the arrow keys
// move between the options with a single tab stop for the whole control —
// the same keyboard model a tab strip offers, without the tab role.

import { useId } from 'react';
import { cn } from '@/lib/cn';

export interface SegmentedOption<T extends string> {
  value: T;
  label: React.ReactNode;
}

interface SegmentedControlProps<T extends string> {
  /** The currently selected value. */
  value: T;
  options: readonly SegmentedOption<T>[];
  onChange: (value: T) => void;
  /** Accessible name of the group — what the options switch. */
  label: string;
  className?: string;
}

export function SegmentedControl<T extends string>({
  value,
  options,
  onChange,
  label,
  className,
}: SegmentedControlProps<T>) {
  // Radios that share a `name` are one group; the id keeps two controls on
  // the same page from stealing each other's selection.
  const group = useId();
  return (
    <fieldset className={cn('m-0 flex min-w-0 items-center gap-1 border-0 p-0', className)}>
      <legend className="sr-only">{label}</legend>
      {options.map((option) => (
        <label key={option.value} className="relative shrink-0 cursor-pointer">
          <input
            type="radio"
            name={group}
            value={option.value}
            checked={value === option.value}
            onChange={() => onChange(option.value)}
            className="peer sr-only"
          />
          <span
            className={cn(
              // The same box the tab strip used: 44px on phones (the
              // touch-target floor), the compact height from lg up.
              'flex h-11 min-w-fit items-center gap-1.5 border-b-2 border-transparent px-3 text-[13px] font-medium text-fg-muted transition-colors duration-150 lg:h-10',
              'hover:text-fg peer-checked:border-accent peer-checked:text-fg',
              // The input is visually hidden, so its focus ring has to be
              // drawn on the visible label.
              'peer-focus-visible:outline peer-focus-visible:outline-2 peer-focus-visible:outline-offset-[-2px] peer-focus-visible:outline-accent',
            )}
          >
            {option.label}
          </span>
        </label>
      ))}
    </fieldset>
  );
}
