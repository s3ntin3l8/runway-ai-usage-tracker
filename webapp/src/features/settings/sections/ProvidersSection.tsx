import { groupProviderFamilies, providerFamilyId } from '@/lib/providerFamilies';
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
  previewAccountUnmerge,
  putDashboardLayout,
  putProviderConfig,
  unmergeProviderAccount,
} from '@/api/endpoints';
import type {
  CredentialProviderView,
  DashboardLayout,
  ProviderConfig,
  UnmergePlan,
} from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { Input } from '@/components/ui/Input';
import { ProviderGlyph } from '@/components/ui/ProviderGlyph';
import { Skeleton } from '@/components/ui/Skeleton';
import { useIsDesktop } from '@/hooks/useMediaQuery';
import { useInvalidateCredentialViews } from '@/hooks/useInvalidateCredentialViews';
import { setPullToRefreshSuspended } from '@/lib/pullToRefresh';
import { useProviderConfigs } from '@/features/home/queries';
import { useCredentialInventory } from './credentials/queries';
import { useDashboardLayout } from '@/features/home/queries';
import { ProviderDetailDialog } from './ProviderDetailDialog';
import { AddProviderWizard } from './AddProviderWizard';
import { UntaggedCredentialsDialog } from '@/features/fleet/UntaggedCredentialsDialog';
import { labelOrMaskedId, maskAccountId } from '@/lib/accountDisplay';
import { errorMessage } from '@/lib/errorMessage';
import { timeAgo } from '@/lib/format';

/**
 * The display name of one of a provider's accounts: its label when it has
 * one, otherwise the masked id. A merged-away account is no longer listed
 * under the provider, so this falls back to the id.
 */
function accountName(provider: ProviderConfig, accountId: string): string {
  const account = provider.accounts.find((a) => a.account_id === accountId);
  return account ? labelOrMaskedId(account) : maskAccountId(accountId);
}

/**
 * The tables a merge moves, in words the operator can read.
 */
const UNMERGE_TABLE_LABELS: Record<string, string> = {
  usage_events: 'usage events',
  latest_usage: 'quota cards',
  latest_usage_contributions: 'quota card sources',
  usage_windows: 'usage windows',
  quota_snapshots: 'quota snapshots',
};

function unmergeSummary(plan: UnmergePlan): string {
  const parts = Object.entries(plan.rows).map(
    ([table, count]) => `${count} ${UNMERGE_TABLE_LABELS[table] ?? table.replaceAll('_', ' ')}`,
  );
  if (plan.credential_sources.length > 0) {
    parts.push(
      `${plan.credential_sources.length} credential source${plan.credential_sources.length === 1 ? '' : 's'}`,
    );
  }
  if (plan.contributions > 0) {
    parts.push(`${plan.contributions} credential bundle${plan.contributions === 1 ? '' : 's'}`);
  }
  if (plan.credential_tags > 0) {
    parts.push(`${plan.credential_tags} tag${plan.credential_tags === 1 ? '' : 's'}`);
  }
  return parts.length > 0 ? `Back to this account: ${parts.join(', ')}.` : 'Nothing to move back.';
}

/**
 * Undo a merge: the account's history, its credential and its name go back to
 * the account they came from.
 *
 * The preview states what goes back and what does not — the duplicates the
 * merge dropped and the stored credential it wiped are gone for good — and the
 * operator reads that before confirming.
 */
function UnmergeButton({
  providerId,
  accountId,
  onUnmerged,
}: {
  providerId: string;
  accountId: string;
  onUnmerged: () => void;
}) {
  const [plan, setPlan] = useState<UnmergePlan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [acknowledge, setAcknowledge] = useState(false);
  // The account is an account again: the fleet, the credential inventory and
  // the provider cards all read that state, so they refresh together.
  const invalidateCredentialViews = useInvalidateCredentialViews();

  const openPreview = async () => {
    setOpen(true);
    setPlan(null);
    setError(null);
    setAcknowledge(false);
    try {
      setPlan(await previewAccountUnmerge(providerId, accountId));
    } catch (previewError) {
      // A merge that cannot be reversed says so here, in place — the operator
      // reads why instead of getting a toast and an empty panel.
      setError(errorMessage(previewError, 'Could not read what the un-merge would do'));
    }
  };

  const run = async () => {
    setBusy(true);
    try {
      await unmergeProviderAccount(providerId, accountId, plan?.total_collisions ? true : false);
      toast.success('Account unmerged');
      setOpen(false);
      invalidateCredentialViews();
      onUnmerged();
    } catch (unmergeError) {
      toast.error(errorMessage(unmergeError, 'Could not un-merge account'));
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <Button variant="secondary" size="sm" onClick={openPreview}>
        Unmerge
      </Button>
    );
  }

  const blocked = (plan?.total_collisions ?? 0) > 0;
  return (
    <div className="flex w-full flex-col gap-2 border-t border-border pt-2">
      {error ? (
        <p className="text-xs text-fg-muted">{error}</p>
      ) : plan ? (
        <>
          <p className="text-xs text-fg-muted">{unmergeSummary(plan)}</p>
          {plan.not_restored.length > 0 ? (
            <p className="text-xs text-fg-muted">
              Not restored: {plan.not_restored.join('; ')}.
            </p>
          ) : null}
          {blocked ? (
            <label className="flex items-center gap-2 text-xs text-fg-muted">
              <input
                type="checkbox"
                checked={acknowledge}
                onChange={(event) => setAcknowledge(event.target.checked)}
              />
              {plan.total_collisions} row{plan.total_collisions === 1 ? '' : 's'} that got in the
              way {plan.total_collisions === 1 ? 'is' : 'are'} replaced by the rows the merge
              moved.
            </label>
          ) : null}
        </>
      ) : (
        <p className="text-xs text-fg-muted">Reading what the un-merge would do…</p>
      )}
      <div className="flex items-center justify-end gap-2">
        <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>
          Cancel
        </Button>
        {error ? null : (
          <Button
            variant="primary"
            size="sm"
            loading={busy}
            disabled={!plan || (blocked && !acknowledge)}
            onClick={run}
          >
            Unmerge
          </Button>
        )}
      </div>
    </div>
  );
}

/**
 * The permanent delete of an empty archived account, with its confirmation.
 * An archived account holds no live credential, so removing the row loses
 * nothing but the account itself. Offered wherever an archived row is listed
 * — including the leftover row of a merged-away account, which is otherwise
 * not restorable.
 */
function PermanentDeleteButton({
  providerId,
  accountId,
  pendingKey,
  setPendingKey,
  onDeleted,
}: {
  providerId: string;
  accountId: string;
  pendingKey: string | null;
  setPendingKey: (key: string | null) => void;
  onDeleted: () => void;
}) {
  const key = `${providerId}/${accountId}`;
  if (pendingKey === key) {
    return (
      <div className="flex w-full items-center justify-end gap-2 border-t border-border pt-2">
        <span className="mr-auto text-xs text-fg-muted">Permanently delete this empty account?</span>
        <Button variant="ghost" size="sm" onClick={() => setPendingKey(null)}>
          Cancel
        </Button>
        <Button
          variant="danger"
          size="sm"
          onClick={async () => {
            try {
              await deleteProviderConfig(providerId, accountId, true);
              setPendingKey(null);
              onDeleted();
              toast.success('Archived account permanently deleted');
            } catch (error) {
              toast.error(errorMessage(error, 'Could not delete account'));
            }
          }}
        >
          Delete permanently
        </Button>
      </div>
    );
  }
  return (
    <Button variant="danger" size="sm" onClick={() => setPendingKey(key)}>
      Delete permanently
    </Button>
  );
}

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
const CREDENTIAL_PROBLEMS: ReadonlySet<string> = new Set(['invalid', 'expired', 'failing']);

function accountCredentialProblems(
  provider: CredentialProviderView | undefined,
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const account of provider?.accounts ?? []) {
    if (CREDENTIAL_PROBLEMS.has(account.status)) {
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
  const familyProviders = useMemo(() => groupProviderFamilies(providers), [providers]);
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
    const bad = p.accounts.find((a) => CREDENTIAL_PROBLEMS.has(a.status));
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
    if (!q) return familyProviders;
    return familyProviders.filter(
      (p) => p.name.toLowerCase().includes(q) || p.provider_id.toLowerCase().includes(q) ||
        providers.some((member) => providerFamilyId(member.provider_id) === p.provider_id && member.name.toLowerCase().includes(q)),
    );
  }, [providers, familyProviders, search]);

  // Apply persisted provider_order from dashboard_layout, falling back to
  // server order. Memoized to keep the dnd-kit sensors stable across renders.
  const ordered = useMemo(() => {
    const saved = layout.data?.provider_order ?? [];
    if (saved.length === 0) return familyProviders;
    const byId = new Map(familyProviders.map((p) => [p.provider_id, p]));
    const result: ProviderConfig[] = [];
    for (const id of saved) {
      const familyId = providerFamilyId(id);
      const p = byId.get(familyId);
      if (p) {
        result.push(p);
        byId.delete(familyId);
      }
    }
    // Append any providers not in the saved order (newly registered).
    for (const p of byId.values()) result.push(p);
    return result;
  }, [familyProviders, layout.data]);

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

  // A retired provider still counts as configured: its accounts are archived,
  // not missing. Without this, a deployment whose only provider was retired
  // would render the fresh-install "No providers configured" empty state and
  // hide the card that says the provider is archived.
  const hasAnyConfig = providers.some(
    (p) => p.account_count > 0 || (p.archived_count ?? 0) > 0,
  );
  const archivedAccounts = providers.flatMap((p) =>
    p.accounts.filter((a) => a.archived).map((account) => ({ provider: p, account })),
  );
  // An account that was merged away is retired by the *merge*, not by an
  // archive: the merge moved its history and its credential to the
  // destination, so it holds nothing and cannot be restored (the API refuses
  // the restore). It gets its own section below, and never a Restore button.
  const archivedOnly = archivedAccounts.filter(({ account }) => !account.merged_into);
  const mergedAccounts = providers.flatMap((p) =>
    (p.merged_accounts ?? []).map((merged) => ({
      provider: p,
      merged,
      // The row the merge left behind (archived, no credential). It cannot be
      // restored, but an empty one can still be cleaned up.
      leftover: p.accounts.find((a) => a.account_id === merged.account_id),
    })),
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
                    family={providers.filter((member) => providerFamilyId(member.provider_id) === p.provider_id).length > 1}
                    credentialProblem={credentialProblems.get(p.provider_id) ?? (p.provider_id === 'anthropic' ? credentialProblems.get('anthropic_console') : undefined)}
                    onOpen={() => setDetailProviderId(
                      providers.find((member) => member.provider_id === p.provider_id)?.provider_id ??
                      providers.find((member) => providerFamilyId(member.provider_id) === p.provider_id)?.provider_id ?? null
                    )}
                  />
                ))}
            </SortableContext>
          </DndContext>
        )}
      </div>

      {archivedOnly.length > 0 ? (
        <section className="mt-6 max-w-2xl" aria-label="Archived provider accounts">
          <h3 className="mb-2 text-sm font-semibold">Archived accounts ({archivedOnly.length})</h3>
          <div className="flex flex-col gap-2">
            {archivedOnly.map(({ provider, account }) => (
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
                      toast.error(errorMessage(error, 'Could not restore account'));
                    }
                  }}
                >
                  Restore
                </Button>
                {account.has_usage_events === false ? (
                  <PermanentDeleteButton
                    providerId={provider.provider_id}
                    accountId={account.account_id}
                    pendingKey={pendingPermanentDeleteKey}
                    setPendingKey={setPendingPermanentDeleteKey}
                    onDeleted={() => configs.refetch()}
                  />
                ) : null}
              </Card>
            ))}
          </div>
        </section>
      ) : null}

      {/* The accounts that were merged away. They are not accounts any more —
          the merge moved their history and their credential to the destination
          — so this list says where each of them went, and offers the one thing
          that can still be done: putting the merge back. */}
      {mergedAccounts.length > 0 ? (
        <section className="mt-6 max-w-2xl" aria-label="Merged provider accounts">
          <h3 className="mb-2 text-sm font-semibold">Merged accounts ({mergedAccounts.length})</h3>
          <div className="flex flex-col gap-2">
            {mergedAccounts.map(({ provider, merged, leftover }) => (
              <Card
                key={`${provider.provider_id}/${merged.account_id}`}
                className="flex flex-wrap items-center gap-3 px-4 py-3"
              >
                <ProviderGlyph providerId={provider.provider_id} name={provider.name} />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-[13px] font-medium">{provider.name}</p>
                  <p className="truncate text-[11px] text-fg-subtle">
                    {accountName(provider, merged.account_id)}
                    {' → '}
                    {accountName(provider, merged.destination_account_id)}
                  </p>
                </div>
                <Badge variant="neutral">Merged</Badge>
                <span className="text-[11px] text-fg-subtle">{timeAgo(merged.merged_at)}</span>
                <UnmergeButton
                  providerId={provider.provider_id}
                  accountId={merged.account_id}
                  onUnmerged={() => configs.refetch()}
                />
                {leftover?.archived && leftover.has_usage_events === false ? (
                  <PermanentDeleteButton
                    providerId={provider.provider_id}
                    accountId={merged.account_id}
                    pendingKey={pendingPermanentDeleteKey}
                    setPendingKey={setPendingPermanentDeleteKey}
                    onDeleted={() => configs.refetch()}
                  />
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
        key={detailProviderId}
        provider={detailProvider}
        connections={detailProvider && providerFamilyId(detailProvider.provider_id) === 'anthropic'
          ? providers.filter((p) => providerFamilyId(p.provider_id) === 'anthropic') : undefined}
        onSelectConnection={setDetailProviderId}
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
  family = false,
  credentialProblem,
  onOpen,
}: {
  provider: ProviderConfig;
  family?: boolean;
  /** `invalid` / `expired` when an account's credentials are rejected or dead. */
  credentialProblem?: string;
  onOpen: () => void;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: provider.provider_id,
  });

  const activeAccounts = provider.accounts.filter((a) => !a.archived);
  // An account that was merged away is listed under "Merged accounts", not
  // here: it is not an archived account the operator can restore.
  const archivedAccounts = provider.accounts.filter((a) => a.archived && !a.merged_into);
  // A provider whose every account is archived is *retired*, not unconfigured:
  // it is set up, the operator just closed it. It must not claim "Not
  // configured" — the account list below shows the same provider under
  // "Archived accounts" with the Restore action.
  const isArchivedOnly = activeAccounts.length === 0 && archivedAccounts.length > 0;
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
            {isArchivedOnly
              ? `Archived · ${archivedAccounts.length} ${
                  archivedAccounts.length === 1 ? 'account' : 'accounts'
                }`
              : provider.account_count === 0
                ? 'Not configured'
                : `${provider.account_count} ${provider.account_count === 1 ? 'account' : 'accounts'} · poll ${
                    provider.effective_poll_interval ?? provider.default_ttl_seconds ?? '—'
                  }s${archivedAccounts.length ? ` · ${archivedAccounts.length} archived` : ''}`}
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
                isArchivedOnly
                  ? 'neutral'
                  : activeAccounts.length === 0
                    ? 'neutral'
                    : allEnabled
                      ? 'accent'
                      : anyEnabled
                        ? 'warning'
                        : 'neutral'
              }
            >
              {family ? 'connections: ' : null}
              {isArchivedOnly
                ? 'archived'
                : activeAccounts.length === 0
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
