import { Table, TBody, TD, TH, THead, TR } from '@/components/ui/Table';
import type { PendingUsageSession } from '@/api/types';
import { PendingUsageSessionRow } from './PendingUsageSessionRow';
import { sessionKey, type AssignOption, type AssignOptionGroup } from './pendingUsageSession';

interface PendingUsageSessionsTableProps {
  items: PendingUsageSession[];
  machineNames: Map<string, string>;
  accounts: Record<string, string>;
  selected: Record<string, PendingUsageSession>;
  assignPending: boolean;
  batchPending: boolean;
  optionGroupsForProvider: (usageProviderId: string) => AssignOptionGroup[];
  validChoice: (usageProviderId: string, value: string | undefined) => string | undefined;
  findOption: (usageProviderId: string, value: string | undefined) => AssignOption | undefined;
  optionNote: (usageProviderId: string, option: AssignOption | undefined) => string | null;
  providerName: (providerId: string) => string;
  onToggleSelected: (key: string, group: PendingUsageSession, checked: boolean) => void;
  onChooseAccount: (key: string, value: string) => void;
  onSetup: (providerId: string, rowKey: string) => void;
  onAssign: (group: PendingUsageSession, chosen: string) => void;
}

export function PendingUsageSessionsTable({
  items,
  machineNames,
  accounts,
  selected,
  assignPending,
  batchPending,
  optionGroupsForProvider,
  validChoice,
  findOption,
  optionNote,
  providerName,
  onToggleSelected,
  onChooseAccount,
  onSetup,
  onAssign,
}: PendingUsageSessionsTableProps) {
  return (
    <div className="mt-3 overflow-hidden rounded border border-border bg-surface-1">
      <Table className="min-w-[900px] text-xs">
        <THead className="bg-surface-2">
          <TR>
            <TH className="w-10"><span className="sr-only">Select</span></TH>
            <TH>Provider</TH>
            <TH>Host</TH>
            <TH>Session</TH>
            <TH>Events · time range</TH>
            <TH>Account</TH>
            <TH><span className="sr-only">Action</span></TH>
          </TR>
        </THead>
        <TBody>
          {items.map((group) => {
            const key = sessionKey(group);
            const chosen = validChoice(group.provider_id, accounts[key]);
            return (
              <PendingUsageSessionRow
                key={key}
                group={group}
                selected={Boolean(selected[key])}
                machineNames={machineNames}
                providerDisplayName={providerName(group.provider_id)}
                optionGroups={optionGroupsForProvider(group.provider_id)}
                chosen={chosen}
                note={optionNote(group.provider_id, findOption(group.provider_id, chosen))}
                assignPending={assignPending}
                batchPending={batchPending}
                onSelectedChange={(checked) => onToggleSelected(key, group, checked)}
                onChooseAccount={(value) => onChooseAccount(key, value)}
                onSetup={() => onSetup(group.provider_id, key)}
                onAssign={() => onAssign(group, chosen!)}
              />
            );
          })}
          {items.length === 0 && (
            <TR><TD colSpan={7} className="py-8 text-center text-fg-muted">No unassigned usage matches these filters.</TD></TR>
          )}
        </TBody>
      </Table>
    </div>
  );
}
