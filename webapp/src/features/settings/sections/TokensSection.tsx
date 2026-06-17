// Token health: every cached credential with expiry state; manual refresh
// for OAuth tokens and cache eviction.

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { KeyRound, RefreshCw, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import { deleteTokenHealth, postTokenRefresh } from '@/api/endpoints';
import type { TokenHealthEntry } from '@/api/types';
import { Badge, type BadgeProps } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { Skeleton } from '@/components/ui/Skeleton';
import { Tooltip } from '@/components/ui/Tooltip';
import { useTokenHealth } from '@/features/home/queries';
import { formatDuration } from '@/lib/format';
import { formatLocalDateTime } from '@/lib/tz';

const STATUS_VARIANT: Record<string, BadgeProps['variant']> = {
  valid: 'ok',
  expiring: 'warning',
  expired: 'critical',
  unknown: 'neutral',
};

export function TokensSection() {
  const health = useTokenHealth();

  if (health.isPending) {
    return (
      <div className="flex flex-col gap-2">
        {Array.from({ length: 3 }, (_, i) => (
          <Skeleton key={i} className="h-16" />
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

  return (
    <div className="flex max-w-2xl flex-col gap-2">
      {tokens.map((t, i) => (
        <TokenRow key={`${t.provider}-${t.account_id}-${i}`} token={t} />
      ))}
    </div>
  );
}

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
    <Card
      className={
        'flex items-center gap-3 px-4 py-3' + (token.redundant ? ' opacity-60' : '')
      }
    >
      <div className="min-w-0 flex-1">
        <p className="truncate text-[13px] font-medium">
          {token.provider}
          <span className="ml-1.5 text-fg-subtle">
            {token.account_label || token.account_id}
          </span>
        </p>
        <p className="truncate text-[11px] text-fg-subtle">
          <Tooltip content={`Credential types: ${typesLabel}`}>
            <span className="cursor-default">{typesLabel}</span>
          </Tooltip>
          {ttlLabel ? ` · ${ttlLabel}` : ''}
          {token.expires_at
            ? ` · expires ${formatLocalDateTime(token.expires_at, {
                month: 'short',
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit',
              })}`
            : ''}
        </p>
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        {sourceName ? (
          <Badge variant="neutral" title="Credential originates from this sidecar">
            {sourceName}
          </Badge>
        ) : null}
        {token.redundant ? (
          <Badge variant="neutral" title="Expired but another healthy credential exists — not blocking collection">
            redundant
          </Badge>
        ) : null}
        <Badge variant={STATUS_VARIANT[token.status] ?? 'neutral'}>{token.status}</Badge>
      </div>
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
    </Card>
  );
}
