import type { ChangeEvent } from 'react';
import { Button } from '@/components/ui/Button';
import type { AssignOptionGroup } from './pendingUsageSession';

type PendingUsageAssignControlsProps = {
  providerId: string;
  providerDisplayName: string;
  optionGroups: AssignOptionGroup[];
  value: string | undefined;
  note: string | null;
  onChoose: (value: string) => void;
  onSetup: () => void;
} & (
  | { variant: 'session'; sessionLabel: string }
  | { variant: 'batch' }
);

function accountOptions(optionGroups: AssignOptionGroup[]) {
  return (
    <>
      <option value="">Choose account…</option>
      {optionGroups.map((group) => (
        <optgroup key={group.label} label={group.label}>
          {group.options.map((option) => (
            <option key={option.value} value={option.value}>
              {option.kind === 'archived' ? `${option.label} · archived` : option.label}
            </option>
          ))}
        </optgroup>
      ))}
      <option value="__add_new__">+ Set up new account…</option>
    </>
  );
}

/** Account select (or setup button) for one session row or one batch provider. */
export function PendingUsageAssignControls(props: PendingUsageAssignControlsProps) {
  const { providerId, providerDisplayName, optionGroups, value, note, onChoose, onSetup } = props;
  const handleChange = (event: ChangeEvent<HTMLSelectElement>) => {
    if (event.target.value === '__add_new__') {
      onSetup();
      return;
    }
    onChoose(event.target.value);
  };

  if (optionGroups.length === 0) {
    if (props.variant === 'batch') {
      return (
        <div className="flex items-center gap-2">
          <span className="text-warning" aria-live="polite">No account configured</span>
          <Button
            size="sm"
            variant="secondary"
            className="h-8 text-xs shrink-0"
            onClick={onSetup}
          >
            + Set up {providerDisplayName}
          </Button>
        </div>
      );
    }
    return (
      <Button
        size="sm"
        variant="secondary"
        className="h-8 text-xs shrink-0"
        onClick={onSetup}
      >
        + Set up {providerDisplayName}
      </Button>
    );
  }

  if (props.variant === 'batch') {
    return (
      <div>
        <select
          aria-label={`Batch account for ${providerId}`}
          className="h-9 w-full rounded-sm border border-edge bg-surface-2 px-2 text-[13px] text-fg"
          value={value ?? ''}
          onChange={handleChange}
        >
          {accountOptions(optionGroups)}
        </select>
        {note && (
          <p className="mt-1 text-[11px] font-normal text-fg-muted">
            {note}
          </p>
        )}
      </div>
    );
  }

  return (
    <div>
      <select
        aria-label={`Account for ${providerId} session ${props.sessionLabel}`}
        className="h-8 min-w-40 max-w-52 rounded-sm border border-edge bg-surface-2 px-2 text-xs"
        value={value ?? ''}
        onChange={handleChange}
      >
        {accountOptions(optionGroups)}
      </select>
      {note && (
        <p className="mt-1 max-w-52 whitespace-normal text-[11px] text-fg-muted">
          {note}
        </p>
      )}
    </div>
  );
}
