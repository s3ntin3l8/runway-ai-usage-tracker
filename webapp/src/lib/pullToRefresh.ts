// Lets a draggable UI nested inside the app-wide pull-to-refresh region
// (AppShell) suspend the gesture for the duration of its own drag, without
// prop-drilling through the router's <Outlet/>. Needed because dnd-kit drag
// handles (e.g. the Home provider grid's reorderable cards) have no separate
// "edit mode" — every card is draggable at all times, and react-simple-pull-
// to-refresh's own touch listeners would otherwise also see the same touch
// sequence and can turn a reorder-drag into a refresh (verified via
// Playwright). Call setPullToRefreshSuspended(true) from a DndContext's
// onDragStart and (false) from onDragEnd/onDragCancel; AppShell reads
// usePullToRefreshSuspended() into PullToRefresh's isPullable prop.
//
// This rides the grain of dnd-kit's own touch activation constraint (delay +
// tolerance) rather than racing it: a fast downward swipe exceeds dnd-kit's
// tolerance before its delay elapses, so dnd-kit bails without ever calling
// onDragStart — pull-to-refresh proceeds untouched. A held-then-dragged
// reorder crosses dnd-kit's activation delay first (~250ms, well under
// react-simple-pull-to-refresh's pullDownThreshold of 67px of movement), so
// the suspend flips before a refresh could visually trigger.
import { useSyncExternalStore } from 'react';

let suspended = false;
const listeners = new Set<() => void>();

export function setPullToRefreshSuspended(next: boolean): void {
  if (suspended === next) return;
  suspended = next;
  listeners.forEach((notify) => notify());
}

export function usePullToRefreshSuspended(): boolean {
  return useSyncExternalStore(
    (notify) => {
      listeners.add(notify);
      return () => listeners.delete(notify);
    },
    () => suspended,
  );
}
