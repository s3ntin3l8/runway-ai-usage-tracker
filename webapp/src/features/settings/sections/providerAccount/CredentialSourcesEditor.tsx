// Credential-source list for one account: try-order, enablement, and whether
// a change applies on every machine.

import { Link } from 'react-router';
import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  TouchSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from '@dnd-kit/core';
import {
  SortableContext,
  arrayMove,
  sortableKeyboardCoordinates,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable';
import type { CredentialSourceSummary } from '@/api/types';
import { buildSidecarNameMap, useSidecars } from '@/features/fleet/queries';
import { setPullToRefreshSuspended } from '@/lib/pullToRefresh';
import { SortableCredentialSourceRow } from './SortableCredentialSourceRow';

export function CredentialSourcesEditor({
  sources,
  onChange,
  allMachines,
  onAllMachinesChange,
}: {
  sources: CredentialSourceSummary[];
  onChange: (sources: CredentialSourceSummary[]) => void;
  allMachines: boolean;
  onAllMachinesChange: (value: boolean) => void;
}) {
  const machineNames = buildSidecarNameMap(useSidecars().data?.sidecars ?? []);
  const sourceSensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );
  const handleSourceDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const oldIndex = sources.findIndex((source) => source.source_id === active.id);
    const newIndex = sources.findIndex((source) => source.source_id === over.id);
    if (oldIndex < 0 || newIndex < 0) return;
    onChange(arrayMove(sources, oldIndex, newIndex));
  };

  return (
    <fieldset className="flex flex-col gap-2 rounded-sm border border-edge p-3">
      <legend className="px-1 text-xs font-medium text-fg-muted">Credential sources</legend>
      <p className="text-[11px] text-fg-subtle">
        Enabled sources are tried in order. Secrets are never shown here.{' '}
        <Link to="/settings/credentials" className="text-accent-text hover:underline">
          See every credential
        </Link>
      </p>
      {sources.some((source) => source.sidecar_id) ? (
        <label className="flex items-center gap-2 text-[11px] text-fg-muted">
          <input
            type="checkbox"
            checked={allMachines}
            onChange={(event) => onAllMachinesChange(event.target.checked)}
          />
          Apply matching credential origins across all machines
        </label>
      ) : null}
      {sources.length === 0 ? (
        <p className="text-[12px] text-fg-muted">Sources will appear after the next credential scan.</p>
      ) : (
        <DndContext
          sensors={sourceSensors}
          collisionDetection={closestCenter}
          onDragStart={() => setPullToRefreshSuspended(true)}
          onDragEnd={(event) => {
            setPullToRefreshSuspended(false);
            handleSourceDragEnd(event);
          }}
          onDragCancel={() => setPullToRefreshSuspended(false)}
        >
          <SortableContext
            items={sources.map((source) => source.source_id)}
            strategy={verticalListSortingStrategy}
          >
            <ul className="divide-y divide-border" aria-label="Credential sources">
              {sources.map((source) => (
                <SortableCredentialSourceRow
                  key={source.source_id}
                  source={source}
                  machineName={source.sidecar_id ? machineNames.get(source.sidecar_id) : undefined}
                  onToggle={(enabled) =>
                    onChange(
                      sources.map((item) =>
                        item.source_id === source.source_id ? { ...item, enabled } : item,
                      ),
                    )
                  }
                />
              ))}
            </ul>
          </SortableContext>
        </DndContext>
      )}
    </fieldset>
  );
}
