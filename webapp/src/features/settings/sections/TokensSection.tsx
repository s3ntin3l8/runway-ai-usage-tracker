// Token health: every cached credential with expiry state; manual refresh
// for OAuth tokens and cache eviction.
//
// Layout: a filter toolbar above a full-width data table. All sorting and
// filtering is client-side — the API returns the full credential list.

import { useMemo, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { ArrowDown, ArrowUp, KeyRound, RefreshCw, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import { deleteTokenHealth, postTokenRefresh } from '@/api/endpoints';
import type { TokenHealthEntry } from '@/api/types';
import { Badge, type BadgeProps } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { EmptyState } from '@/components/ui/EmptyState';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select';
import { Skeleton } from '@/components/ui/Skeleton';
import { Table, TBody, TD, TH, THead, TR } from '@/components/ui/Table';
import { Tooltip } from '@/components/ui/Tooltip';
import { useTokenHealth } from '@/features/home/queries';
import { cn } from '@/lib/cn';
import { formatDuration } from '@/lib/format';
import { formatLocalDateTime } from '@/lib/tz';

// ─── Constants ─────────────────────────────────────────────────────────────

const ALL = '__all__';

const STATUS_VARIANT: Record<string, BadgeProps['variant']> = {
  valid: 'ok',
  expiring: 'warning',
  expired: 'critical',
  unknown: 'neutral',
};

// Lower = more severe; used for ascending-severity sort.
const STATUS_SEVERITY: Record<string, number> = {
  expired: 0,
  expiring: 1,
  unknown: 2,
  valid: 3,
};

// ─── Sort types ─────────────────────────────────────────────────────────────

type SortKey = 'provider' | 'identifier' | 'origin' | 'validity';
type SortDir = 'asc' | 'desc';

// ─── SortableTH ─────────────────────────────────────────────────────────────
// Adapted from SessionsTable.tsx — same aria-sort + arrow pattern, but wired to
// the local client-side sort state rather than a server-side query param.

function SortableTH({
  label,
  sortKey,
  sort,
  onSort,
  className,
}: {
  label: string;
  sortKey: SortKey;
  sort: { by: SortKey; dir: SortDir };
  onSort: (key: SortKey) => void;
  className?: string;
}) {
  const active = sort.by === sortKey;
  return (
    <TH
      className={className}
      aria-sort={active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none'}
    >
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        className={cn('flex items-center gap-1 hover:text-fg', active && 'text-fg')}
      >
        {label}
        {active ? (
          sort.dir === 'asc' ? (
            <ArrowUp className="size-3" aria-hidden />
          ) : (
            <ArrowDown className="size-3" aria-hidden />
          )
        ) : null}
      </button>
    </TH>
  );
}

// ─── Main section ────────────────────────────────────────────────────────────

export function TokensSection() {
  const health = useTokenHealth();

  // Sort state — default: validity ascending (expired first).
  const [sort, setSort] = useState<{ by: SortKey; dir: SortDir }>({
    by: 'validity',
    dir: 'asc',
  });

  // Filter state.
  const [filterProvider, setFilterProvider] = useState(ALL);
  const [filterStatus, setFilterStatus] = useState(ALL);
  const [filterOrigin, setFilterOrigin] = useState(ALL);

  const handleSort = (key: SortKey) => {
    setSort((prev) =>
      prev.by === key
        ? { by: key, dir: prev.dir === 'asc' ? 'desc' : 'asc' }
        : { by: key, dir: key === 'validity' ? 'asc' : 'desc' },
    );
  };

  // ── Loading / error / empty states ────────────────────────────────────────

  if (health.isPending) {
    return (
      <div className="flex flex-col gap-2">
        {Array.from({ length: 3 }, (_, i) => (
          <Skeleton key={i} className="h-10" />
        ))}
      </div>
    );
  }

  if (health.isError) {
    return (
      <EmptyState
        icon={KeyRound}
        title="Token health unavailable"
        description={health.error.message}
      />
    );
  }

  const tokens = health.data?.tokens ?? [];
  if (tokens.length === 0) {
    return (
      <EmptyState
        icon={KeyRound}
        title="No cached credentials"
        description="Tokens appear here once providers or sidecars hand credentials to the server."
      />
    );
  }

  // ── Derive filter options from data ───────────────────────────────────────

  const providers = [...new Set(tokens.map((t) => t.provider))].sort();
  const statuses = [...new Set(tokens.map((t) => t.status))].sort();
  // Unique origin labels: sidecar names + 'config' bucket for server-local.
  const origins = [
    ...new Set(
      tokens.map((t) =>
        t.source_name && t.source_name !== 'config' ? t.source_name : 'config',
      ),
    ),
  ].sort();

  return (
    <FilteredTable
      tokens={tokens}
      sort={sort}
      onSort={handleSort}
      filterProvider={filterProvider}
      setFilterProvider={setFilterProvider}
      filterStatus={filterStatus}
      setFilterStatus={setFilterStatus}
      filterOrigin={filterOrigin}
      setFilterOrigin={setFilterOrigin}
      providers={providers}
      statuses={statuses}
      origins={origins}
    />
  );
}

// ─── FilteredTable ───────────────────────────────────────────────────────────
// Separated so useMemo can close over stable props without re-creating on every
// outer render (health data refetch won't recreate the filter/sort functions).

function FilteredTable({
  tokens,
  sort,
  onSort,
  filterProvider,
  setFilterProvider,
  filterStatus,
  setFilterStatus,
  filterOrigin,
  setFilterOrigin,
  providers,
  statuses,
  origins,
}: {
  tokens: TokenHealthEntry[];
  sort: { by: SortKey; dir: SortDir };
  onSort: (key: SortKey) => void;
  filterProvider: string;
  setFilterProvider: (v: string) => void;
  filterStatus: string;
  setFilterStatus: (v: string) => void;
  filterOrigin: string;
  setFilterOrigin: (v: string) => void;
  providers: string[];
  statuses: string[];
  origins: string[];
}) {
  // Filter then sort — both are derived from the full tokens array via useMemo.
  const visible = useMemo(() => {
    const filtered = tokens.filter((t) => {
      if (filterProvider !== ALL && t.provider !== filterProvider) return false;
      if (filterStatus !== ALL && t.status !== filterStatus) return false;
      if (filterOrigin !== ALL) {
        const originLabel =
          t.source_name && t.source_name !== 'config' ? t.source_name : 'config';
        if (originLabel !== filterOrigin) return false;
      }
      return true;
    });

    return [...filtered].sort((a, b) => {
      const mul = sort.dir === 'asc' ? 1 : -1;
      switch (sort.by) {
        case 'provider':
          return mul * a.provider.localeCompare(b.provider);
        case 'identifier': {
          const la = (a.account_label || a.account_id).toLowerCase();
          const lb = (b.account_label || b.account_id).toLowerCase();
          return mul * la.localeCompare(lb);
        }
        case 'origin': {
          const oa = a.source_name && a.source_name !== 'config' ? a.source_name : '';
          const ob = b.source_name && b.source_name !== 'config' ? b.source_name : '';
          return mul * oa.localeCompare(ob);
        }
        case 'validity': {
          const sa = STATUS_SEVERITY[a.status] ?? 99;
          const sb = STATUS_SEVERITY[b.status] ?? 99;
          return mul * (sa - sb);
        }
        default:
          return 0;
      }
    });
  }, [tokens, sort, filterProvider, filterStatus, filterOrigin]);

  return (
    <div className="flex flex-col gap-3">
      {/* Filter toolbar */}
      <div className="flex flex-wrap items-center gap-2">
        <Select
          value={filterProvider}
          onValueChange={(v) => { if (v) setFilterProvider(v); }}
        >
          <SelectTrigger className="h-8 w-40 text-xs" aria-label="Filter by provider">
            <SelectValue placeholder="All providers" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>All providers</SelectItem>
            {providers.map((p) => (
              <SelectItem key={p} value={p}>
                {p}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <Select
          value={filterStatus}
          onValueChange={(v) => { if (v) setFilterStatus(v); }}
        >
          <SelectTrigger className="h-8 w-36 text-xs" aria-label="Filter by status">
            <SelectValue placeholder="All statuses" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>All statuses</SelectItem>
            {statuses.map((s) => (
              <SelectItem key={s} value={s}>
                {s}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <Select
          value={filterOrigin}
          onValueChange={(v) => { if (v) setFilterOrigin(v); }}
        >
          <SelectTrigger className="h-8 w-44 text-xs" aria-label="Filter by origin">
            <SelectValue placeholder="All origins" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>All origins</SelectItem>
            {origins.map((o) => (
              <SelectItem key={o} value={o}>
                {o}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        {(filterProvider !== ALL || filterStatus !== ALL || filterOrigin !== ALL) && (
          <button
            type="button"
            className="text-xs text-fg-muted hover:text-fg"
            onClick={() => {
              setFilterProvider(ALL);
              setFilterStatus(ALL);
              setFilterOrigin(ALL);
            }}
          >
            Clear filters
          </button>
        )}
      </div>

      {/* Table */}
      {visible.length === 0 ? (
        <p className="text-[13px] text-fg-subtle">No credentials match the current filters.</p>
      ) : (
        <Table>
          <THead>
            <TR>
              <SortableTH label="Provider" sortKey="provider" sort={sort} onSort={onSort} />
              <SortableTH label="Identifier" sortKey="identifier" sort={sort} onSort={onSort} />
              <TH>Detail</TH>
              <SortableTH label="Origin" sortKey="origin" sort={sort} onSort={onSort} />
              <SortableTH label="Validity" sortKey="validity" sort={sort} onSort={onSort} />
              <TH className="w-20 text-right">Actions</TH>
            </TR>
          </THead>
          <TBody>
            {visible.map((t, i) => (
              <TokenRow key={`${t.provider}-${t.account_id}-${i}`} token={t} />
            ))}
          </TBody>
        </Table>
      )}
    </div>
  );
}

// ─── TokenRow ────────────────────────────────────────────────────────────────

function TokenRow({ token }: { token: TokenHealthEntry }) {
  const queryClient = useQueryClient();
  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ['system', 'token-health'] });

  const refresh = useMutation({
    mutationFn: () => postTokenRefresh(token.provider, token.account_id),
    onSuccess: () => {
      toast.success('Token refreshed');
      invalidate();
    },
    onError: (err) => toast.error(`Refresh failed: ${err.message}`),
  });

  const remove = useMutation({
    mutationFn: () => deleteTokenHealth(token.provider, token.account_id),
    onSuccess: () => {
      toast.success('Token removed from cache');
      invalidate();
    },
    onError: (err) => toast.error(err.message),
  });

  // TTL label: how long until the next poll/refresh, shown when > 0.
  const ttlLabel =
    token.ttl_remaining_seconds && token.ttl_remaining_seconds > 0
      ? `TTL: ${formatDuration(token.ttl_remaining_seconds * 1000)}`
      : null;

  // Source badge: omit for server-local config, surface sidecar names.
  const sourceName =
    token.source_name && token.source_name !== 'config' ? token.source_name : null;

  const typesLabel = (token.token_types ?? []).join(', ') || '—';

  return (
    <TR className={token.redundant ? 'opacity-60' : undefined}>
      {/* Provider */}
      <TD className="font-medium">{token.provider}</TD>

      {/* Identifier */}
      <TD className="text-fg-subtle">{token.account_label || token.account_id}</TD>

      {/* Detail */}
      <TD className="text-fg-subtle">
        <Tooltip content={`Credential types: ${typesLabel}`}>
          <span className="cursor-default">{typesLabel}</span>
        </Tooltip>
      </TD>

      {/* Origin */}
      <TD>
        {sourceName ? (
          <Badge variant="neutral" title="Credential originates from this sidecar">
            {sourceName}
          </Badge>
        ) : (
          <span className="text-xs text-fg-subtle">config</span>
        )}
      </TD>

      {/* Validity */}
      <TD>
        <div className="flex flex-wrap items-center gap-1.5">
          {token.redundant ? (
            <Badge
              variant="neutral"
              title="Expired but another healthy credential exists — not blocking collection"
            >
              redundant
            </Badge>
          ) : null}
          <Badge variant={STATUS_VARIANT[token.status] ?? 'neutral'}>{token.status}</Badge>
          {ttlLabel ? (
            <span className="text-xs text-fg-subtle">{ttlLabel}</span>
          ) : null}
          {token.expires_at ? (
            <span className="text-xs text-fg-subtle">
              expires{' '}
              {formatLocalDateTime(token.expires_at, {
                month: 'short',
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit',
              })}
            </span>
          ) : null}
        </div>
      </TD>

      {/* Actions */}
      <TD className="text-right">
        <div className="flex items-center justify-end gap-1">
          {token.can_refresh ? (
            <Button
              size="icon-sm"
              variant="ghost"
              aria-label="Refresh token"
              title="Refresh token"
              onClick={() => refresh.mutate()}
              loading={refresh.isPending}
            >
              <RefreshCw className="size-3.5" />
            </Button>
          ) : null}
          <Button
            size="icon-sm"
            variant="ghost"
            aria-label="Remove from cache"
            title="Remove from cache"
            onClick={() => remove.mutate()}
            loading={remove.isPending}
            className="text-critical hover:bg-critical-muted"
          >
            <Trash2 className="size-3.5" />
          </Button>
        </div>
      </TD>
    </TR>
  );
}
