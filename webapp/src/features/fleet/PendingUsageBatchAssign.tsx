import { Button } from '@/components/ui/Button';
import type { PendingUsageSession } from '@/api/types';
import { PendingUsageAssignControls } from './PendingUsageAssignControls';
import type { AssignOption, AssignOptionGroup } from './pendingUsageSession';

interface PendingUsageBatchAssignProps {
  selectedProviders: string[];
  selectedGroups: PendingUsageSession[];
  batchAccounts: Record<string, string>;
  batchPending: boolean;
  selectedEventCount: number;
  optionGroupsForProvider: (usageProviderId: string) => AssignOptionGroup[];
  validChoice: (usageProviderId: string, value: string | undefined) => string | undefined;
  findOption: (usageProviderId: string, value: string | undefined) => AssignOption | undefined;
  optionNote: (usageProviderId: string, option: AssignOption | undefined) => string | null;
  providerName: (providerId: string) => string;
  onSetup: (providerId: string) => void;
  onChooseAccount: (providerId: string, value: string) => void;
  onCancel: () => void;
  onApply: () => void;
}

export function PendingUsageBatchAssign({
  selectedProviders,
  selectedGroups,
  batchAccounts,
  batchPending,
  selectedEventCount,
  optionGroupsForProvider,
  validChoice,
  findOption,
  optionNote,
  providerName,
  onSetup,
  onChooseAccount,
  onCancel,
  onApply,
}: PendingUsageBatchAssignProps) {
  return (
    <div className="space-y-3">
      {selectedProviders.map((providerId) => {
        const groups = selectedGroups.filter((group) => group.provider_id === providerId);
        const count = groups.reduce((sum, group) => sum + group.event_count, 0);
        const chosenBatch = validChoice(providerId, batchAccounts[providerId]);
        return (
          <div
            key={providerId}
            className="grid gap-1 text-xs font-medium text-fg-muted sm:grid-cols-[minmax(9rem,1fr)_2fr] sm:items-center"
          >
            <span>{providerId} · {count} events</span>
            <PendingUsageAssignControls
              variant="batch"
              providerId={providerId}
              providerDisplayName={providerName(providerId)}
              optionGroups={optionGroupsForProvider(providerId)}
              value={chosenBatch}
              note={optionNote(providerId, findOption(providerId, chosenBatch))}
              onChoose={(value) => onChooseAccount(providerId, value)}
              onSetup={() => onSetup(providerId)}
            />
          </div>
        );
      })}
      <div className="flex justify-end gap-2 pt-2">
        <Button variant="secondary" onClick={onCancel}>Cancel</Button>
        <Button
          variant="primary"
          disabled={!selectedProviders.length || selectedProviders.some((providerId) => !validChoice(providerId, batchAccounts[providerId])) || batchPending}
          loading={batchPending}
          onClick={onApply}
        >
          Assign {selectedEventCount} events
        </Button>
      </div>
    </div>
  );
}
