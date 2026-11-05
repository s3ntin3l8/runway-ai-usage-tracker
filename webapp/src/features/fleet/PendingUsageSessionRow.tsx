import { Button } from '@/components/ui/Button';
import { TD, TR } from '@/components/ui/Table';
import type { PendingUsageSession } from '@/api/types';
import { formatLocalDateTime } from '@/lib/tz';
import { PendingUsageAssignControls } from './PendingUsageAssignControls';
import { sidecarLabel } from './queries';
import { groupLabel, type AssignOptionGroup } from './pendingUsageSession';

interface PendingUsageSessionRowProps {
  group: PendingUsageSession;
  selected: boolean;
  machineNames: Map<string, string>;
  providerDisplayName: string;
  optionGroups: AssignOptionGroup[];
  chosen: string | undefined;
  note: string | null;
  assignPending: boolean;
  batchPending: boolean;
  onSelectedChange: (checked: boolean) => void;
  onChooseAccount: (value: string) => void;
  onSetup: () => void;
  onAssign: () => void;
}

export function PendingUsageSessionRow({
  group,
  selected,
  machineNames,
  providerDisplayName,
  optionGroups,
  chosen,
  note,
  assignPending,
  batchPending,
  onSelectedChange,
  onChooseAccount,
  onSetup,
  onAssign,
}: PendingUsageSessionRowProps) {
  const label = groupLabel(group);
  const models = group.model_ids.length ? group.model_ids.join(', ') : 'Unknown model';
  const first = formatLocalDateTime(group.first_ts);
  const last = formatLocalDateTime(group.last_ts);
  return (
    <TR className={selected ? 'bg-accent-muted/40' : undefined}>
      <TD>
        <input
          type="checkbox"
          aria-label={`Select ${group.provider_id} on ${group.sidecar_id} session ${label}`}
          checked={selected}
          onChange={(event) => onSelectedChange(event.target.checked)}
        />
      </TD>
      <TD className="font-medium">{group.provider_id}</TD>
      <TD className="max-w-36 truncate" title={group.sidecar_id}>{sidecarLabel(machineNames, group.sidecar_id)}</TD>
      <TD className="max-w-64">
        <details className="group">
          <summary className="cursor-pointer truncate font-medium" title={label}>
            {group.session_id ? `Session ${group.session_id}` : `No session ID · event ${group.event_ids[0]}`}
          </summary>
          <p className="mt-1 break-all text-[11px] text-fg-muted">Models: {models}</p>
        </details>
      </TD>
      <TD className="whitespace-nowrap text-fg-muted">
        <span className="font-medium text-fg">{group.event_count}</span> · {first}
        {first !== last ? ` – ${last}` : ''}
      </TD>
      <TD>
        <PendingUsageAssignControls
          variant="session"
          providerId={group.provider_id}
          sessionLabel={label}
          providerDisplayName={providerDisplayName}
          optionGroups={optionGroups}
          value={chosen}
          note={note}
          onChoose={onChooseAccount}
          onSetup={onSetup}
        />
      </TD>
      <TD>
        <Button
          size="sm"
          variant="primary"
          disabled={!chosen || assignPending || batchPending}
          loading={assignPending}
          onClick={onAssign}
        >
          {group.session_id ? 'Assign' : 'Assign event'}
        </Button>
      </TD>
    </TR>
  );
}
