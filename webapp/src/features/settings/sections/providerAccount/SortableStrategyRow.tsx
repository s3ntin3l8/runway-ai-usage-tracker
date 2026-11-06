// One draggable collection-strategy row. The parent owns order and enabled state.

import { useSortable } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { GripVertical } from 'lucide-react';
import { Switch } from '@/components/ui/Switch';
import type { StrategyEntry } from './initStrategies';

export function SortableStrategyRow({
  strategy,
  onToggle,
}: {
  strategy: StrategyEntry;
  onToggle: (enabled: boolean) => void;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: strategy.id,
  });

  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      className={`flex items-center justify-between rounded-sm px-1 py-1.5 ${
        isDragging ? 'z-10 opacity-60' : ''
      }`}
    >
      <div
        {...attributes}
        {...listeners}
        className="flex items-center gap-2 touch-none"
        aria-label={'Reorder ' + strategy.label}
      >
        <GripVertical className="size-3.5 text-fg-muted" />
        <span className="text-[13px]">{strategy.label}</span>
      </div>
      <Switch
        checked={strategy.enabled}
        onCheckedChange={onToggle}
        aria-label={strategy.label}
      />
    </div>
  );
}
