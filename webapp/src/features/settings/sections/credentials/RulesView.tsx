// Assignment rules: "this credential origin → that account". A rule only matters when a
// machine can't identify a credential on its own; removing one makes the credential
// reappear under "Needs mapping" if it still has no local identity.

import { useState } from 'react';
import { useSearchParams } from 'react-router';
import { useMutation, useQuery } from '@tanstack/react-query';
import { ListChecks, Search, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import { deleteCredentialTag, fetchCredentialTags } from '@/api/endpoints';
import type { CredentialTag } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { ConfirmDialog } from '@/components/ui/ConfirmDialog';
import { EmptyState } from '@/components/ui/EmptyState';
import { Input } from '@/components/ui/Input';
import { Skeleton } from '@/components/ui/Skeleton';
import { buildSidecarNameMap, useSidecars } from '@/features/fleet/queries';
import { useInvalidateCredentialViews } from '@/hooks/useInvalidateCredentialViews';
import { maskAccountId } from '@/lib/accountDisplay';

function ruleKey(t: CredentialTag): string {
  return `${t.provider_id}/${t.credential_origin}/${t.sidecar_id ?? '*'}`;
}

const GROUPS = ['provider', 'machine', 'account'] as const;
type Group = (typeof GROUPS)[number];
const GROUP_LABEL: Record<Group, string> = {
  provider: 'Provider',
  machine: 'Machine',
  account: 'Account',
};
const SCOPES = ['all', 'machine', 'all_machines'] as const;
type Scope = (typeof SCOPES)[number];
const SCOPE_LABEL: Record<Scope, string> = {
  all: 'All',
  machine: 'One machine',
  all_machines: 'All machines',
};

/** "Codex CLI · auth.json" — falls back to the raw origin for legacy, unparseable ones. */
function originTitle(t: CredentialTag): string {
  const label = t.origin_label;
  if (!label || label === 'Sidecar credential') return t.credential_origin;
  return t.origin_app && label !== 'Browser cookie' ? `${t.origin_app} · ${label}` : label;
}

function pick<T extends string>(value: string | null, allowed: readonly T[], fallback: T): T {
  return (allowed as readonly string[]).includes(value ?? '') ? (value as T) : fallback;
}

function Segmented<T extends string>({
  label,
  value,
  options,
  labels,
  onChange,
}: {
  label: string;
  value: T;
  options: readonly T[];
  labels: Record<T, string>;
  onChange: (next: T) => void;
}) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="text-[11px] text-fg-subtle">{label}</span>
      <div role="group" aria-label={label} className="flex rounded-md border border-edge p-0.5">
        {options.map((o) => (
          <button
            key={o}
            type="button"
            aria-pressed={value === o}
            onClick={() => onChange(o)}
            className={`rounded-sm px-2 py-0.5 text-[11px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ${
              value === o ? 'bg-surface-2 font-medium text-fg' : 'text-fg-muted hover:text-fg'
            }`}
          >
            {labels[o]}
          </button>
        ))}
      </div>
    </div>
  );
}

export function RulesView({ providerNames }: { providerNames?: Record<string, string> }) {
  const [params, setParams] = useSearchParams();
  const group = pick(params.get('rgroup'), GROUPS, 'provider');
  const scope = pick(params.get('rscope'), SCOPES, 'all');
  const query = params.get('rq') ?? '';
  const setParam = (key: string, value: string, fallback: string) => {
    const copy = new URLSearchParams(params);
    if (value === fallback) copy.delete(key);
    else copy.set(key, value);
    setParams(copy, { replace: true });
  };

  const invalidate = useInvalidateCredentialViews();
  const rules = useQuery({ queryKey: ['fleet', 'credential_tags'], queryFn: fetchCredentialTags });
  const sidecars = useSidecars();
  const names = buildSidecarNameMap(sidecars.data?.sidecars ?? []);
  const [removing, setRemoving] = useState<CredentialTag | null>(null);

  const remove = useMutation({
    mutationFn: (t: CredentialTag) => deleteCredentialTag(t),
    onSuccess: () => {
      toast.success('Rule removed');
      setRemoving(null);
      invalidate();
    },
    onError: (err: Error) => toast.error(err.message),
  });

  const all = rules.data?.items ?? [];
  const providerName = (t: CredentialTag) => providerNames?.[t.provider_id] ?? t.provider_id;
  const machineName = (t: CredentialTag) =>
    t.sidecar_id ? (names.get(t.sidecar_id) ?? t.sidecar_id) : 'All machines';

  const groups = (() => {
    const needle = query.trim().toLowerCase();
    const keyOf = {
      provider: providerName,
      machine: machineName,
      account: (t: CredentialTag) => maskAccountId(t.account_id),
    }[group];
    const matches = all.filter((t) => {
      if (scope === 'machine' && !t.sidecar_id) return false;
      if (scope === 'all_machines' && t.sidecar_id) return false;
      if (!needle) return true;
      return [
        providerName(t),
        t.provider_id,
        t.account_id,
        t.credential_origin,
        t.origin_label,
        t.origin_app,
        t.origin_path,
        machineName(t),
      ].some((v) => v?.toLowerCase().includes(needle));
    });
    const byKey = new Map<string, CredentialTag[]>();
    for (const t of matches) byKey.set(keyOf(t), [...(byKey.get(keyOf(t)) ?? []), t]);
    return [...byKey.entries()].sort(([a], [b]) => a.localeCompare(b));
  })();
  const shown = groups.reduce((n, [, rows]) => n + rows.length, 0);

  return (
    <Card className="p-4">
      <h3 className="text-sm font-semibold">Assignment rules</h3>
      <p className="text-[11px] text-fg-subtle">
        When a machine can't tell which account a credential belongs to, a rule files it under
        the account you chose. A rule applies to one machine or to all of them.
      </p>
      {rules.isPending ? (
        <Skeleton className="mt-3 h-12 w-full" />
      ) : rules.isError ? (
        <p className="mt-3 text-[12px] text-critical">Couldn't load rules: {rules.error.message}</p>
      ) : all.length === 0 ? (
        <EmptyState
          icon={ListChecks}
          title="No assignment rules"
          description="Rules appear here once you assign a credential to an account."
        />
      ) : (
        <>
          <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2">
            <div className="relative w-full max-w-60">
              <Search
                className="pointer-events-none absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-fg-subtle"
                aria-hidden
              />
              <Input
                type="search"
                aria-label="Search rules"
                placeholder="Search rules"
                className="h-8 pl-7 text-[12px]"
                value={query}
                onChange={(e) => setParam('rq', e.target.value, '')}
              />
            </div>
            <Segmented
              label="Group by"
              value={group}
              options={GROUPS}
              labels={GROUP_LABEL}
              onChange={(v) => setParam('rgroup', v, 'provider')}
            />
            <Segmented
              label="Applies to"
              value={scope}
              options={SCOPES}
              labels={SCOPE_LABEL}
              onChange={(v) => setParam('rscope', v, 'all')}
            />
          </div>
          {shown === 0 ? (
            <p className="mt-3 text-[12px] text-fg-muted">
              No rules match these filters ({all.length} total).
            </p>
          ) : (
            <div className="mt-2 space-y-3">
              {groups.map(([name, rows]) => (
                <section key={name} aria-label={`${name} rules`}>
                  <h4 className="text-[11px] font-semibold uppercase tracking-wide text-fg-subtle">
                    {name} <span className="font-normal">· {rows.length}</span>
                  </h4>
                  <ul className="divide-y divide-edge" aria-label={`${name} assignment rules`}>
                    {rows.map((t) => (
                      <li
                        key={ruleKey(t)}
                        className="flex items-center justify-between gap-3 py-2"
                      >
                        <div className="min-w-0">
                          <p className="truncate text-[13px]">
                            {group !== 'provider' ? (
                              <span className="font-medium">{providerName(t)} </span>
                            ) : null}
                            <span
                              className="font-medium"
                              title={t.origin_path ?? t.credential_origin}
                            >
                              {originTitle(t)}
                            </span>
                            {' → '}
                            <span>{maskAccountId(t.account_id)}</span>
                            {t.target_provider_id && t.target_provider_id !== t.provider_id && (
                              <span className="text-fg-muted"> ({t.target_provider_id})</span>
                            )}
                          </p>
                          <p className="truncate text-[11px] text-fg-subtle">
                            {t.sidecar_id
                              ? `on ${names.get(t.sidecar_id) ?? t.sidecar_id}`
                              : 'on all machines'}
                          </p>
                          {t.stale ? (
                            <p className="text-[11px] text-warning">
                              {t.last_matched_at
                                ? `No credential seen since ${new Date(t.last_matched_at).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })}`
                                : 'No credential has matched this rule'}
                              {' — Data Health can remove it.'}
                            </p>
                          ) : null}
                        </div>
                        <Button
                          variant="danger-ghost"
                          size="icon-sm"
                          aria-label={`Remove rule ${ruleKey(t)}`}
                          onClick={() => setRemoving(t)}
                        >
                          <Trash2 className="size-3.5" aria-hidden />
                        </Button>
                      </li>
                    ))}
                  </ul>
                </section>
              ))}
            </div>
          )}
        </>
      )}
      <ConfirmDialog
        open={removing !== null}
        onOpenChange={(o) => !o && setRemoving(null)}
        title="Remove this rule?"
        description={'The credential goes back to "Needs mapping" if its machine still can\'t identify it.'}
        pending={remove.isPending}
        onConfirm={() => removing && remove.mutate(removing)}
      />
    </Card>
  );
}
