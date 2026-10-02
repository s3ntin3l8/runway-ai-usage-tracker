// Provider configuration with multi-account rendering.

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
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
import { toast } from 'sonner';
import {
  deleteProviderConfig,
  fetchUntaggedCredentials,
  putDashboardLayout,
  putProviderConfig,
} from '@/api/endpoints';
import type { CredentialProviderView, DashboardLayout, ProviderConfig } from '@/api/types';
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
import { useCredentialInventory } from './credentials/queries';
import { useDashboardLayout } from '@/features/home/queries';
import { ProviderDetailDialog } from './ProviderDetailDialog';
import { AddProviderWizard } from './AddProviderWizard';
import { UntaggedCredentialsDialog } from '@/features/fleet/UntaggedCredentialsDialog';
import { labelOrMaskedId } from '@/lib/accountDisplay';

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

interface UseProvidersResult {
  configs: ReturnType<typeof useProviderConfigs>;
  layout: ReturnType<typeof useDashboardLayout>;
  saveOrder: ReturnType<typeof useMutation<{ status: string }, Error, string[]>>;
}

function useProviders(): UseProvidersResult {
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
  return { configs, layout, saveOrder };
}

/** `{account_id: status}` for a provider's accounts whose credential is expired or rejected. */
function accountCredentialProblems(
  provider: CredentialProviderView | undefined,
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const account of provider?.accounts ?? []) {
    if (account.status === 'invalid' || account.status === 'expired') {
      out[account.account_id] = account.status;
    }
  }
  return out;
}

export function ProvidersSection() {
  const { configs, layout, saveOrder } = useProviders();
  return <ProvidersSectionV2 configs={configs} layout={layout} saveOrder={saveOrder} />;
}

// ---------------------------------------------------------------------------
// Card grid + per-account dialog shell.
// ---------------------------------------------------------------------------

function ProvidersSectionV2({
  configs,
  layout,
  saveOrder,
}: {
  configs: ReturnType<typeof useProviderConfigs>;
  layout: ReturnType<typeof useDashboardLayout>;
  saveOrder: UseProvidersResult['saveOrder'];
}) {
  const providers = configs.data?.providers ?? [];
  const isDesktop = useIsDesktop();
  // Hold only the provider_id — resolving the object from `configs.data`
  // each render keeps the open dialog in sync after invalidateQueries
  // (a one-shot object snapshot froze the switch/badges on stale state).
  const [detailProviderId, setDetailProviderId] = useState<string | null>(null);
  const [pendingPermanentDeleteKey, setPendingPermanentDeleteKey] = useState<string | null>(null);
  const detailProvider = providers.find((p) => p.provider_id === detailProviderId) ?? null;
  // Wizard (#287) — null = closed, otherwise the provider we pre-scoped to
  // (undefined/null = open at step 1 with no pre-scope).
  const [wizardScope, setWizardScope] = useState<ProviderConfig | null | undefined>(undefined);
  const [assignmentOpen, setAssignmentOpen] = useState(false);
  const pendingCredentials = useQuery({
    queryKey: ['fleet', 'untagged_credentials', 'all'],
    queryFn: () => fetchUntaggedCredentials(),
  });
  // Drives the key/cookie badges so a rejected or expired credential doesn't read as
  // green. Admin-gated and best-effort: without it the badges simply stay neutral-ok.
  const credentialInventory = useCredentialInventory();
  const credentialProblems = new Map<string, string>();
  for (const p of credentialInventory.data?.providers ?? []) {
    const bad = p.accounts.find((a) => a.status === 'invalid' || a.status === 'expired');
    if (bad) credentialProblems.set(p.provider_id, bad.status);
  }

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
  const archivedAccounts = providers.flatMap((p) =>
    p.accounts.filter((a) => a.archived).map((account) => ({ provider: p, account })),
  );

  return (
    <>
      <div className="flex max-w-2xl flex-col gap-3">
        {(pendingCredentials.data?.items.length ?? 0) > 0 ? (
          <Card className="flex items-center justify-between gap-3 p-3">
            <div className="min-w-0">
              <p className="text-[13px] font-semibold">Credentials need an account</p>
              <p className="text-[11px] text-fg-subtle">
                {pendingCredentials.data?.items.length} discovered credential{pendingCredentials.data?.items.length === 1 ? '' : 's'} could not be matched to a stable identity.
              </p>
            </div>
            <Button variant="primary" size="sm" onClick={() => setAssignmentOpen(true)}>
              Assign accounts
            </Button>
          </Card>
        ) : null}
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
                    credentialProblem={credentialProblems.get(p.provider_id)}
                    onOpen={() => setDetailProviderId(p.provider_id)}
                  />
                ))}
            </SortableContext>
          </DndContext>
        )}
      </div>

      {archivedAccounts.length > 0 ? (
        <section className="mt-6 max-w-2xl" aria-label="Archived provider accounts">
          <h3 className="mb-2 text-sm font-semibold">Archived accounts ({archivedAccounts.length})</h3>
          <div className="flex flex-col gap-2">
            {archivedAccounts.map(({ provider, account }) => (
              <Card key={`${provider.provider_id}/${account.account_id}`} className="flex flex-wrap items-center gap-3 px-4 py-3">
                <ProviderGlyph providerId={provider.provider_id} name={provider.name} />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-[13px] font-medium">{provider.name}</p>
                  <p className="truncate text-[11px] text-fg-subtle">{labelOrMaskedId(account)}</p>
                </div>
                <Badge variant="neutral">Archived</Badge>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={async () => {
                    try {
                      await putProviderConfig(provider.provider_id, account.account_id, { archived: false });
                      await configs.refetch();
                    } catch (error) {
                      toast.error(error instanceof Error ? error.message : 'Could not restore account');
                    }
                  }}
                >
                  Restore
                </Button>
                {account.has_usage_events === false ? (
                  pendingPermanentDeleteKey === `${provider.provider_id}/${account.account_id}` ? (
                    <div className="flex w-full items-center justify-end gap-2 border-t border-border pt-2">
                      <span className="mr-auto text-xs text-fg-muted">Permanently delete this empty account?</span>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setPendingPermanentDeleteKey(null)}
                      >
                        Cancel
                      </Button>
                      <Button
                        variant="danger"
                        size="sm"
                        onClick={async () => {
                          try {
                            await deleteProviderConfig(provider.provider_id, account.account_id, true);
                            setPendingPermanentDeleteKey(null);
                            await configs.refetch();
                            toast.success('Archived account permanently deleted');
                          } catch (error) {
                            toast.error(error instanceof Error ? error.message : 'Could not delete account');
                          }
                        }}
                      >
                        Delete permanently
                      </Button>
                    </div>
                  ) : (
                    <Button
                      variant="danger"
                      size="sm"
                      onClick={() => setPendingPermanentDeleteKey(`${provider.provider_id}/${account.account_id}`)}
                    >
                      Delete permanently
                    </Button>
                  )
                ) : null}
              </Card>
            ))}
          </div>
        </section>
      ) : null}

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
        onClose={() => setDetailProviderId(null)}
        onAccountDeleted={(providerId) => {
          if (detailProviderId === providerId) {
            // Refresh so the dialog's account list reflects the deletion.
            configs.refetch();
          }
        }}
        onAddAccount={(p) => setWizardScope(p)}
        credentialProblems={accountCredentialProblems(
          credentialInventory.data?.providers.find((p) => p.provider_id === detailProviderId),
        )}
      />

      <UntaggedCredentialsDialog
        open={assignmentOpen}
        onClose={() => setAssignmentOpen(false)}
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
  credentialProblem,
  onOpen,
}: {
  provider: ProviderConfig;
  /** `invalid` / `expired` when an account's credentials are rejected or dead. */
  credentialProblem?: string;
  onOpen: () => void;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: provider.provider_id,
  });

  const activeAccounts = provider.accounts.filter((a) => !a.archived);
  const hasKey = activeAccounts.some((a) => a.api_key_set);
  const hasCookie = activeAccounts.some((a) => a.session_cookie_set);
  const allEnabled = activeAccounts.length > 0 && activeAccounts.every((a) => a.enabled);
  const anyEnabled = activeAccounts.some((a) => a.enabled);
  // Discovered-only = every account came from token_cache / latest_usage
  // (no provider_configs row). Passive providers (antigravity, …) never get
  // a config row — show "auto" instead of "unconfigured" / "enabled".
  const onlyDiscovered =
    activeAccounts.length > 0 &&
    activeAccounts.every((a) => a.source === 'discovered');

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
                }s${provider.archived_count ? ` · ${provider.archived_count} archived` : ''}`}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {hasKey ? (
            <Badge
              variant={credentialProblem ? 'critical' : 'ok'}
              title={credentialProblem ? `A credential is ${credentialProblem} — see Credentials` : undefined}
            >
              key
            </Badge>
          ) : null}
          {hasCookie ? (
            <Badge
              variant={credentialProblem ? 'critical' : 'ok'}
              title={credentialProblem ? `A credential is ${credentialProblem} — see Credentials` : undefined}
            >
              cookie
            </Badge>
          ) : null}
          {onlyDiscovered ? (
            <Badge variant="ok">auto</Badge>
          ) : (
            <Badge
              variant={
                activeAccounts.length === 0
                  ? 'neutral'
                  : allEnabled
                    ? 'accent'
                    : anyEnabled
                      ? 'warning'
                      : 'neutral'
              }
            >
              {activeAccounts.length === 0
                ? 'unconfigured'
                : allEnabled
                  ? 'enabled'
                  : anyEnabled
                    ? 'partial'
                    : 'disabled'}
            </Badge>
          )}
        </div>
      </button>
    </Card>
  );
}
