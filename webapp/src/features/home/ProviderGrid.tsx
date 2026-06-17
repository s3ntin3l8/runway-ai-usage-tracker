// All-providers grid: compact cards, drag-to-reorder (dnd-kit), persisted
// via PUT /system/dashboard-layout. Keyboard + touch sensors included;
// pointer drags need 8px of travel so taps stay taps.

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
  rectSortingStrategy,
  sortableKeyboardCoordinates,
  useSortable,
} from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { useNavigate } from 'react-router';
import { Badge } from '@/components/ui/Badge';
import { Card } from '@/components/ui/Card';
import { Countdown } from '@/components/ui/Countdown';
import { Gauge } from '@/components/ui/Gauge';
import { ProviderGlyph } from '@/components/ui/ProviderGlyph';
import { StatusDot } from '@/components/ui/StatusDot';
import { formatPct, timeAgo } from '@/lib/format';
import { cardPct, cardStatus, chipLabel, windowLabel } from '@/lib/quota';
import { providerPath } from './AtRiskRail';
import type { RiskItem } from './risk';

interface ProviderGridProps {
  items: RiskItem[]; // already in layout order
  providerNames: Map<string, string>;
  onReorder: (orderedKeys: string[]) => void;
}

export function ProviderGrid({ items, providerNames, onReorder }: ProviderGridProps) {
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const keys = items.map((i) => i.key);
    const next = arrayMove(keys, keys.indexOf(String(active.id)), keys.indexOf(String(over.id)));
    onReorder(next);
  };

  return (
    <section aria-label="All providers" className="flex flex-col gap-2">
      <h2 className="text-xs font-semibold tracking-wide text-fg-subtle uppercase">Providers</h2>
      <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
        <SortableContext items={items.map((i) => i.key)} strategy={rectSortingStrategy}>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
            {items.map((item) => (
              <SortableProviderCard key={item.key} item={item} providerNames={providerNames} />
            ))}
          </div>
        </SortableContext>
      </DndContext>
    </section>
  );
}

function SortableProviderCard({
  item,
  providerNames,
}: {
  item: RiskItem;
  providerNames: Map<string, string>;
}) {
  const navigate = useNavigate();
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: item.key,
  });
  const { entry, status } = item;
  const gauge = entry.critical_gauge;
  const name = providerNames.get(entry.provider_id) ?? entry.provider_id;
  const secondaries = entry.secondary_limits.slice(0, 3);
  const overflow = entry.secondary_limits.length - secondaries.length;
  // Show an account identity when there's a meaningful label or a non-default
  // account_id, so users with multiple accounts can tell cards apart at a glance.
  const accountLabel =
    gauge.account_label ??
    (entry.account_id && entry.account_id !== 'default' ? entry.account_id : null);

  return (
    <Card
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      {...attributes}
      {...listeners}
      role="button"
      tabIndex={0}
      aria-label={
        accountLabel
          ? `${name} (${accountLabel}), ${formatPct(cardPct(gauge))} used`
          : `${name}, ${formatPct(cardPct(gauge))} used`
      }
      onClick={() => {
        if (!isDragging) navigate(providerPath(entry.provider_id, entry.account_id));
      }}
      onKeyDown={(e) => {
        if (e.key === 'Enter') navigate(providerPath(entry.provider_id, entry.account_id));
      }}
      className={
        'cursor-pointer touch-manipulation p-3.5 transition-colors duration-150 hover:border-edge-strong ' +
        (isDragging ? 'z-10 opacity-80 shadow-lg' : '')
      }
    >
      <div className="flex items-center gap-2.5">
        <ProviderGlyph providerId={entry.provider_id} name={name} className="size-6 text-[10px]" />
        <div className="min-w-0 flex-1">
          <p className="truncate text-[13px] font-medium">{name}</p>
          {accountLabel ? (
            <p className="truncate text-[11px] text-fg-subtle">{accountLabel}</p>
          ) : null}
        </div>
        {gauge.tier ? <Badge variant="outline">{gauge.tier}</Badge> : null}
        <StatusDot status={status} pulse={status === 'critical'} />
      </div>
      <div className="mt-3 flex items-baseline justify-between gap-2">
        <span className="font-mono text-lg font-semibold tabular">
          {formatPct(cardPct(gauge))}
        </span>
        <span className="truncate text-[11px] text-fg-subtle">
          {gauge.service_name}
          {windowLabel(gauge) ? ` · ${windowLabel(gauge)}` : ''}
        </span>
      </div>
      <Gauge pct={cardPct(gauge)} status={status} className="mt-1.5" />
      {secondaries.length > 0 ? (
        <div className="mt-2.5 flex flex-wrap gap-1">
          {secondaries.map((card, i) => (
            <Badge key={`${card.service_name}-${i}`} variant={cardStatus(card)}>
              {chipLabel(card, entry.secondary_limits)}
              {cardPct(card) != null ? ` ${Math.round(cardPct(card)!)}%` : ''}
            </Badge>
          ))}
          {overflow > 0 ? <Badge variant="neutral">+{overflow}</Badge> : null}
        </div>
      ) : null}
      <div className="mt-2.5 flex items-center justify-between text-[11px] text-fg-subtle">
        <Countdown until={gauge.reset_at} className="text-[11px]" />
        <span>{timeAgo(gauge.updated_at)}</span>
      </div>
    </Card>
  );
}
