// Provider configuration: per-account settings with multi-account rendering.
// When the `?providers=v2` URL param is set, renders the new card-grid +
// per-account dialog shell (Issues #286 / #287 wizard targets this). When
// absent, falls back to the legacy flat-list single-account form (one
// ProviderConfig row per provider, no per-account breakdown). Rollback =
// drop the URL param.
//
// Backend already exposes `accounts: ProviderAccount[]` and `account_count`
// on every row (PR #281 hardening + #286 follow-up), so the v2 UI consumes
// the same response shape — the only diff is which shell renders.

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
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
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { Plus, Search } from 'lucide-react';
import { useSearchParams } from 'react-router';
import { toast } from 'sonner';
import { putDashboardLayout } from '@/api/endpoints';
import type { DashboardLayout, ProviderConfig } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { Input } from '@/components/ui/Input';
import { ProviderGlyph } from '@/components/ui/ProviderGlyph';
import { Skeleton } from '@/components/ui/Skeleton';
import { useIsDesktop } from '@/hooks/useMediaQuery';
import { setPullToRefreshSuspended } from '@/lib/pullToRefresh';
import { useProviderConfigs } from '@/features/home/queries';
import { useDashboardLayout } from '@/features/home/queries';
import { ProviderDetailDialog } from './ProviderDetailDialog';
import { LegacyEditDialog } from './LegacyEditDialog';
import { AddProviderWizard } from './AddProviderWizard';

export function reorderItems<T>(
  items: T[],
  activeId: string,
  overId: string,
  // Default reads `.id` (used by the strategy reorder — `StrategyEntry` has
  // an `id` field). Provider reorder passes `(p) => p.provider_id` so the
  // v2 grid uses the right key.
  getId: (s: T) => string = (s) => (s as { id?: string }).id ?? '',
): T[] {
  const oldIndex = items.findIndex((s) => getId(s) === activeId);
  const newIndex = items.findIndex((s) => getId(s) === overId);
  if (oldIndex === -1 || newIndex === -1) return items;
  return arrayMove(items, oldIndex, newIndex);
}

interface UseV2ProvidersResult {
  enabled: boolean;
  configs: ReturnType<typeof useProviderConfigs>;
  layout: ReturnType<typeof useDashboardLayout>;
  saveOrder: ReturnType<typeof useMutation<{ status: string }, Error, string[]>>;
}

/** Hook that wraps `?providers=v2` gating + the providers/layout queries. */
export function useV2Providers(): UseV2ProvidersResult {
  const [searchParams] = useSearchParams();
  const enabled = searchParams.get('providers') === 'v2';
  const configs = useProviderConfigs();
  const layout = useDashboardLayout();
  const queryClient = useQueryClient();
  const saveOrder = useMutation({
    mutationFn: (orderedProviderIds: string[]) =>
      putDashboardLayout({
        provider_order: orderedProviderIds,
        card_orders: layout.data?.card_orders ?? {},
      }),
    onMutate: async (orderedProviderIds) => {
      // Optimistic update so the grid order reflects the drop immediately.
      await queryClient.cancelQueries({ queryKey: ['system', 'dashboard-layout'] });
      queryClient.setQueryData(['system', 'dashboard-layout'], (prev: DashboardLayout | undefined) => ({
        provider_order: orderedProviderIds,
        card_orders: prev?.card_orders ?? {},
      }));
    },
    onError: () => {
      toast.error('Could not save provider order');
      queryClient.invalidateQueries({ queryKey: ['system', 'dashboard-layout'] });
    },
  });
  return { enabled, configs, layout, saveOrder };
}

export function ProvidersSection() {
  const { enabled: v2, configs, layout, saveOrder } = useV2Providers();

  if (v2) {
    return <ProvidersSectionV2 configs={configs} layout={layout} saveOrder={saveOrder} />;
  }
  return <ProvidersSectionLegacy configs={configs} />;
}

// ---------------------------------------------------------------------------
// Legacy single-account form (unchanged behaviour, kept for the rollback path).
// The dialog / form internals are isolated in `ProviderDetailDialog` and
// `ProviderAccountDialog` for the v2 UI; this path keeps the original
// `ProviderForm` rendering a single row keyed by `account_id="default"`.
// ---------------------------------------------------------------------------

function ProvidersSectionLegacy({
  configs,
}: {
  configs: ReturnType<typeof useProviderConfigs>;
}) {
  const [editing, setEditing] = useState<ProviderConfig | null>(null);

  if (configs.isPending) {
    return (
      <div className="flex flex-col gap-2">
        {Array.from({ length: 5 }, (_, i) => (
          <Skeleton key={i} className="h-16" />
        ))}
      </div>
    );
  }

  return (
    <div className="flex max-w-2xl flex-col gap-2">
      {(configs.data?.providers ?? []).map((p) => (
        <Card
          key={p.provider_id}
          role="button"
          tabIndex={0}
          onClick={() => setEditing(p)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') setEditing(p);
          }}
          className="flex cursor-pointer items-center gap-3 px-4 py-3 transition-colors duration-150 hover:border-edge-strong"
        >
          <ProviderGlyph providerId={p.provider_id} name={p.name} />
          <div className="min-w-0 flex-1">
            <p className="truncate text-[13px] font-medium">{p.name}</p>
            <p className="truncate text-[11px] text-fg-subtle">
              {p.account_label || p.provider_id} · poll{' '}
              {p.effective_poll_interval ?? p.default_ttl_seconds ?? '—'}s
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            {p.api_key_set ? <Badge variant="ok">key</Badge> : null}
            {p.session_cookie_set ? <Badge variant="ok">cookie</Badge> : null}
            <Badge variant={p.enabled ? 'accent' : 'neutral'}>
              {p.enabled ? 'enabled' : 'disabled'}
            </Badge>
          </div>
        </Card>
      ))}

      {/* Legacy edit dialog — kept verbatim so the rollback path is the
          exact previous build. Wired through the multi-account PUT endpoint
          with account_id="default" for single-account users. */}
      <LegacyEditDialog
        editing={editing}
        onClose={() => setEditing(null)}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// v2: empty-canvas card grid + per-account dialog shell.
// ---------------------------------------------------------------------------

function ProvidersSectionV2({
  configs,
  layout,
  saveOrder,
}: {
  configs: ReturnType<typeof useProviderConfigs>;
  layout: ReturnType<typeof useDashboardLayout>;
  saveOrder: UseV2ProvidersResult['saveOrder'];
}) {
  const providers = configs.data?.providers ?? [];
  const isDesktop = useIsDesktop();
  const [detailProvider, setDetailProvider] = useState<ProviderConfig | null>(null);
  // Wizard (#287) — null = closed, otherwise the provider we pre-scoped to
  // (undefined/null = open at step 1 with no pre-scope).
  const [wizardScope, setWizardScope] = useState<ProviderConfig | null | undefined>(undefined);

  // Build a Map<provider_id, Set<account_id>> for the wizard's
  // defense-in-depth 409 detection (the API does the same check; this lets
  // the UI short-circuit before round-tripping).
  const existingAccountIdsByProvider = useMemo(() => {
    const map = new Map<string, Set<string>>();
    for (const p of providers) {
      map.set(
        p.provider_id,
        new Set(p.accounts.map((a) => a.account_id)),
      );
    }
    return map;
  }, [providers]);

  // Filter strip
  const [search, setSearch] = useState('');
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return providers;
    return providers.filter(
      (p) => p.name.toLowerCase().includes(q) || p.provider_id.toLowerCase().includes(q),
    );
  }, [providers, search]);

  // Apply persisted provider_order from dashboard_layout, falling back to
  // server order. Memoized to keep the dnd-kit sensors stable across renders.
  const ordered = useMemo(() => {
    const saved = layout.data?.provider_order ?? [];
    if (saved.length === 0) return providers;
    const byId = new Map(providers.map((p) => [p.provider_id, p]));
    const result: ProviderConfig[] = [];
    for (const id of saved) {
      const p = byId.get(id);
      if (p) {
        result.push(p);
        byId.delete(id);
      }
    }
    // Append any providers not in the saved order (newly registered).
    for (const p of byId.values()) result.push(p);
    return result;
  }, [providers, layout.data]);

  // Sensors for drag-to-reorder on the provider cards.
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const handleDragEnd = useCallback(
    (event: DragEndEvent) => {
      setPullToRefreshSuspended(false);
      const { active, over } = event;
      if (!over || active.id === over.id) return;
      const next = reorderItems(
        ordered,
        String(active.id),
        String(over.id),
        (p) => p.provider_id,
      );
      saveOrder.mutate(next.map((p) => p.provider_id));
    },
    [ordered, saveOrder],
  );

  // Safety net: dnd-kit normally clears the suspend flag on onDragEnd /
  // onDragCancel, but a mid-drag unmount skips both — prevent permanently
  // stuck pull-to-refresh.
  useEffect(() => () => setPullToRefreshSuspended(false), []);

  if (configs.isPending) {
    return (
      <div className="flex flex-col gap-2">
        {Array.from({ length: 5 }, (_, i) => (
          <Skeleton key={i} className="h-16" />
        ))}
      </div>
    );
  }

  const hasAnyConfig = providers.some((p) => p.account_count > 0);

  return (
    <>
      <div className="flex max-w-2xl flex-col gap-3">
        {providers.length > 5 && (
          <div className="relative">
            <Search
              className="pointer-events-none absolute top-1/2 left-3 size-3.5 -translate-y-1/2 text-fg-subtle"
              aria-hidden
            />
            <Input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search providers…"
              aria-label="Search providers"
              className="pl-9"
            />
          </div>
        )}

        {!hasAnyConfig ? (
          // Fresh install: the registry returns every provider with
          // `account_count=0`, so `filtered.length === 0` is true even
          // though `providers` isn't. Render the global "configure your
          // first" empty state instead of falling through to the
          // search-match state. The CTA opens the wizard (#287).
          <Card className="py-2">
            <EmptyState
              icon={Plus}
              title="No providers configured"
              description="Add your first provider to start tracking AI usage."
              action={
                <Button variant="primary" onClick={() => setWizardScope(null)}>
                  <Plus className="size-3.5" />
                  Add provider
                </Button>
              }
            />
          </Card>
        ) : filtered.length === 0 ? (
          <Card className="py-2">
            <EmptyState
              title={`No providers match "${search}"`}
              action={
                <Button variant="ghost" size="sm" onClick={() => setSearch('')}>
                  Clear search
                </Button>
              }
            />
          </Card>        ) : (
          <DndContext
            sensors={sensors}
            collisionDetection={closestCenter}
            onDragStart={() => setPullToRefreshSuspended(true)}
            onDragEnd={handleDragEnd}
            onDragCancel={() => setPullToRefreshSuspended(false)}
          >
            <SortableContext items={ordered.map((p) => p.provider_id)} strategy={verticalListSortingStrategy}>
              {ordered
                .filter((p) => filtered.includes(p))
                .map((p) => (
                  <SortableProviderCard
                    key={p.provider_id}
                    provider={p}
                    onOpen={() => setDetailProvider(p)}
                  />
                ))}
            </SortableContext>
          </DndContext>
        )}
      </div>

      {/* Add provider CTA — sticky on mobile (above bottom nav), inline
          top-right on desktop. Gated on `hasAnyConfig` so the fresh-install
          path (EmptyState at :307) only shows one Add button; once at least
          one provider is configured, the sticky footer takes over. Both
          buttons open the wizard at step 1 (#287). */}
      {hasAnyConfig ? (
        <Button
          variant="primary"
          size={isDesktop ? 'md' : 'lg'}
          className={isDesktop ? 'mt-3 self-start' : 'fixed inset-x-4 bottom-4 z-30 shadow-lg'}
          onClick={() => setWizardScope(null)}
        >
          <Plus className="size-4" />
          Add provider
        </Button>
      ) : null}

      <ProviderDetailDialog
        provider={detailProvider}
        onClose={() => setDetailProvider(null)}
        onAccountDeleted={(providerId) => {
          if (detailProvider?.provider_id === providerId) {
            // Refresh so the dialog's account list reflects the deletion.
            configs.refetch();
          }
        }}
        onAddAccount={(p) => setWizardScope(p)}
      />

      {wizardScope !== undefined ? (
        <AddProviderWizard
          preScopedProvider={wizardScope}
          providers={providers}
          existingAccountIdsByProvider={existingAccountIdsByProvider}
          onClose={() => {
            setWizardScope(undefined);
            configs.refetch();
          }}
        />
      ) : null}
    </>
  );
}

function SortableProviderCard({
  provider,
  onOpen,
}: {
  provider: ProviderConfig;
  onOpen: () => void;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: provider.provider_id,
  });

  const hasKey = provider.api_key_set;
  const hasCookie = provider.session_cookie_set;
  const allEnabled = provider.accounts.every((a: ProviderConfig['accounts'][number]) => a.enabled);
  const anyEnabled = provider.accounts.some((a: ProviderConfig['accounts'][number]) => a.enabled);

  return (
    <Card
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      className={`flex items-center gap-3 px-4 py-3 ${isDragging ? 'z-10 opacity-60' : ''}`}
    >
      <button
        type="button"
        {...attributes}
        {...listeners}
        aria-label={`Reorder ${provider.name}`}
        className="touch-none text-fg-muted hover:text-fg"
      >
        <span className="sr-only">Drag to reorder</span>
        <svg
          width="14"
          height="14"
          viewBox="0 0 14 14"
          fill="none"
          xmlns="http://www.w3.org/2000/svg"
          aria-hidden
        >
          <circle cx="4" cy="3" r="1" fill="currentColor" />
          <circle cx="4" cy="7" r="1" fill="currentColor" />
          <circle cx="4" cy="11" r="1" fill="currentColor" />
          <circle cx="10" cy="3" r="1" fill="currentColor" />
          <circle cx="10" cy="7" r="1" fill="currentColor" />
          <circle cx="10" cy="11" r="1" fill="currentColor" />
        </svg>
      </button>
      <button
        type="button"
        onClick={onOpen}
        className="flex flex-1 cursor-pointer items-center gap-3 text-left"
      >
        <ProviderGlyph providerId={provider.provider_id} name={provider.name} />
        <div className="min-w-0 flex-1">
          <p className="truncate text-[13px] font-medium">{provider.name}</p>
          <p className="truncate text-[11px] text-fg-subtle">
            {provider.account_count === 0
              ? 'Not configured'
              : `${provider.account_count} ${provider.account_count === 1 ? 'account' : 'accounts'} · poll ${
                  provider.effective_poll_interval ?? provider.default_ttl_seconds ?? '—'
                }s`}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {hasKey ? <Badge variant="ok">key</Badge> : null}
          {hasCookie ? <Badge variant="ok">cookie</Badge> : null}
          <Badge
            variant={
              provider.accounts.length === 0
                ? 'neutral'
                : allEnabled
                  ? 'accent'
                  : anyEnabled
                    ? 'warning'
                    : 'neutral'
            }
          >
            {provider.accounts.length === 0
              ? 'unconfigured'
              : allEnabled
                ? 'enabled'
                : anyEnabled
                  ? 'partial'
                  : 'disabled'}
          </Badge>
        </div>
      </button>
    </Card>
  );
}

// Re-exported here so legacy tests that imported the old `reorderStrategies`
// keep working without churning the test file along with the section rewrite.
export { reorderItems as reorderStrategies } from './ProvidersSection';
