import { arrayMove } from '@dnd-kit/sortable';

export function reorderItems<T>(
  items: T[],
  activeId: string,
  overId: string,
  // Default reads `.id` (used by the strategy reorder — `StrategyEntry` has
  // an `id` field). Provider reorder passes `(p) => p.provider_id` so the
  // provider grid uses the right key.
  getId: (s: T) => string = (s) => (s as { id?: string }).id ?? '',
): T[] {
  const oldIndex = items.findIndex((s) => getId(s) === activeId);
  const newIndex = items.findIndex((s) => getId(s) === overId);
  if (oldIndex === -1 || newIndex === -1) return items;
  return arrayMove(items, oldIndex, newIndex);
}
