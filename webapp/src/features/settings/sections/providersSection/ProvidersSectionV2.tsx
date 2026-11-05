import { useCallback, useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
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
import { SortableContext, sortableKeyboardCoordinates, verticalListSortingStrategy } from '@dnd-kit/sortable';
import { Plus, Search } from 'lucide-react';
import { fetchUntaggedCredentials } from '@/api/endpoints';
import type { ProviderConfig } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { Input } from '@/components/ui/Input';
import { Skeleton } from '@/components/ui/Skeleton';
import { useIsDesktop } from '@/hooks/useMediaQuery';
import { setPullToRefreshSuspended } from '@/lib/pullToRefresh';
import { groupProviderFamilies, providerFamilyId } from '@/lib/providerFamilies';
import { useCredentialInventory } from '../credentials/queries';
import { AddProviderWizard } from '../AddProviderWizard';
import { ProviderDetailDialog } from '../ProviderDetailDialog';
import { UntaggedCredentialsDialog } from '@/features/fleet/UntaggedCredentialsDialog';
import { CREDENTIAL_PROBLEMS, accountCredentialProblems } from './accountCredentialProblems';
import { ArchivedAccountsSection } from './ArchivedAccountsSection';
import { MergedAccountsSection } from './MergedAccountsSection';
import { reorderItems } from './reorderItems';
import { SortableProviderCard } from './SortableProviderCard';
import type { UseProvidersResult } from './useProviders';

// ---------------------------------------------------------------------------
// Card grid + per-account dialog shell.
// ---------------------------------------------------------------------------

export function ProvidersSectionV2({
  configs,
  layout,
  saveOrder,
}: {
  configs: UseProvidersResult['configs'];
  layout: UseProvidersResult['layout'];
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

      <ArchivedAccountsSection
        archivedOnly={archivedOnly}
        pendingPermanentDeleteKey={pendingPermanentDeleteKey}
        setPendingPermanentDeleteKey={setPendingPermanentDeleteKey}
        configs={configs}
      />

      <MergedAccountsSection
        mergedAccounts={mergedAccounts}
        pendingPermanentDeleteKey={pendingPermanentDeleteKey}
        setPendingPermanentDeleteKey={setPendingPermanentDeleteKey}
        configs={configs}
      />

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
