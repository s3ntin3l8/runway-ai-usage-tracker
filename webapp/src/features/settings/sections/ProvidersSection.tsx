// Provider configuration: enable/disable, credentials, account label, poll
// interval, per-strategy toggles. Credentials are write-only (server stores
// them encrypted and only reports *_set flags).

export function reorderStrategies<T extends { id: string }>(
  items: T[],
  activeId: string,
  overId: string,
): T[] {
  const ids = items.map((s) => s.id);
  const oldIndex = ids.indexOf(activeId);
  const newIndex = ids.indexOf(overId);
  if (oldIndex === -1 || newIndex === -1) return items;
  return arrayMove(items, oldIndex, newIndex);
}

import { useCallback, useEffect, useRef, useState } from 'react';
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
import { Copy, ExternalLink, GripVertical, LogIn, LogOut } from 'lucide-react';
import { toast } from 'sonner';
import {
  getGitHubOAuthStatus,
  initGitHubOAuth,
  logoutGitHub,
  pollGitHubOAuth,
  putProviderConfig,
  type ProviderConfigUpdate,
} from '@/api/endpoints';
import type { ProviderConfig } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { Countdown } from '@/components/ui/Countdown';
import { HelperText, Input, Label } from '@/components/ui/Input';
import { ProviderGlyph } from '@/components/ui/ProviderGlyph';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';
import { Skeleton } from '@/components/ui/Skeleton';
import { Switch } from '@/components/ui/Switch';
import { setPullToRefreshSuspended } from '@/lib/pullToRefresh';
import { useProviderConfigs } from '@/features/home/queries';

type FlowState =
  | { phase: 'idle' }
  | {
      phase: 'pending';
      deviceCode: string;
      userCode: string;
      verificationUri: string;
      expiresAt: string;
      pollInterval: number;
    }
  | { phase: 'error'; message: string };

export function ProvidersSection() {
  const configs = useProviderConfigs();
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

      <ResponsiveDialog
        open={editing !== null}
        onOpenChange={(open) => {
          if (!open) setEditing(null);
        }}
        title={editing?.name ?? ''}
        description="Configuration is stored encrypted on the server."
      >
        {editing ? (
          <ProviderForm
            key={editing.provider_id}
            provider={editing}
            onSaved={() => setEditing(null)}
          />
        ) : null}
      </ResponsiveDialog>
    </div>
  );
}

function ProviderForm({ provider, onSaved }: { provider: ProviderConfig; onSaved: () => void }) {
  const queryClient = useQueryClient();
  const [enabled, setEnabled] = useState(provider.enabled ?? true);
  const [apiKey, setApiKey] = useState('');
  const [cookie, setCookie] = useState('');
  const [label, setLabel] = useState(provider.account_label ?? '');
  const [pollInterval, setPollInterval] = useState(
    provider.poll_interval_seconds != null ? String(provider.poll_interval_seconds) : '',
  );
  const [strategies, setStrategies] = useState(
    (provider.collection_strategies ?? provider.supported_strategies ?? []).map((s) => ({
      id: s.id,
      enabled: s.enabled,
      label: (s as { label?: string }).label ?? s.id,
    })),
  );

  const save = useMutation({
    mutationFn: () => {
      const body: ProviderConfigUpdate = {
        enabled,
        account_label: label,
        poll_interval_seconds: pollInterval.trim() === '' ? null : Number(pollInterval),
        collection_strategies: strategies.map(({ id, enabled: on }) => ({ id, enabled: on })),
      };
      // Only send credentials the user actually typed — empty string means
      // "clear" server-side, absence means "keep".
      if (apiKey !== '') body.api_key = apiKey;
      if (cookie !== '') body.session_cookie = cookie;
      return putProviderConfig(provider.provider_id, body);
    },
    onSuccess: () => {
      toast.success(`${provider.name} saved — collection triggered`);
      queryClient.invalidateQueries({ queryKey: ['system', 'provider-configs'] });
      queryClient.invalidateQueries({ queryKey: ['usage'] });
      onSaved();
    },
    onError: (err) => toast.error(err.message),
  });

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const handleDragEnd = (event: DragEndEvent) => {
    setPullToRefreshSuspended(false);
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    setStrategies(reorderStrategies(strategies, String(active.id), String(over.id)));
  };

  // Safety net: onDragEnd/onDragCancel normally clear the suspend flag, but a
  // mid-drag unmount skips both — prevent permanently stuck pull-to-refresh.
  useEffect(() => () => setPullToRefreshSuspended(false), []);

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
      className="flex flex-col gap-4"
    >
      <div className="flex items-center justify-between">
        <Label htmlFor="prov-enabled">Collection enabled</Label>
        <Switch id="prov-enabled" checked={enabled} onCheckedChange={setEnabled} />
      </div>

      {provider.provider_id === 'github' ? (
        <div className="flex flex-col gap-1.5">
          <Label>GitHub login</Label>
          <GitHubLoginSection />
        </div>
      ) : null}

      {provider.supports_api_key ? (
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="prov-key">{provider.api_key_label || 'API key'}</Label>
          <Input
            id="prov-key"
            type="password"
            autoComplete="off"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={provider.api_key_set ? '••••••••  (set — leave blank to keep)' : ''}
          />
          {provider.api_key_help ? <HelperText>{provider.api_key_help}</HelperText> : null}
        </div>
      ) : null}

      {provider.supports_session_cookie ? (
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="prov-cookie">{provider.session_cookie_label || 'Session cookie'}</Label>
          <Input
            id="prov-cookie"
            type="password"
            autoComplete="off"
            value={cookie}
            onChange={(e) => setCookie(e.target.value)}
            placeholder={provider.session_cookie_set ? '••••••••  (set — leave blank to keep)' : ''}
          />
          {provider.session_cookie_help ? (
            <HelperText>{provider.session_cookie_help}</HelperText>
          ) : null}
        </div>
      ) : null}

      <div className="grid grid-cols-2 gap-3">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="prov-label">Account label</Label>
          <Input id="prov-label" value={label} onChange={(e) => setLabel(e.target.value)} />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="prov-poll">Poll interval (s)</Label>
          <Input
            id="prov-poll"
            type="number"
            inputMode="numeric"
            min={30}
            value={pollInterval}
            onChange={(e) => setPollInterval(e.target.value)}
            placeholder={`default ${provider.effective_poll_interval ?? ''}`}
          />
        </div>
      </div>

      {strategies.length > 0 ? (
        <fieldset className="flex flex-col gap-1 rounded-sm border border-edge p-3">
          <legend className="px-1 text-xs font-medium text-fg-muted">Collection strategies</legend>
          <DndContext
            sensors={sensors}
            collisionDetection={closestCenter}
            onDragStart={() => setPullToRefreshSuspended(true)}
            onDragEnd={handleDragEnd}
            onDragCancel={() => setPullToRefreshSuspended(false)}
          >
            <SortableContext items={strategies.map((s) => s.id)} strategy={verticalListSortingStrategy}>
              {strategies.map((s) => (
                <SortableStrategyRow
                  key={s.id}
                  strategy={s}
                  onToggle={(enabled) =>
                    setStrategies((prev) =>
                      prev.map((x) => (x.id === s.id ? { ...x, enabled } : x)),
                    )
                  }
                />
              ))}
            </SortableContext>
          </DndContext>
        </fieldset>
      ) : null}

      <Button type="submit" variant="primary" loading={save.isPending}>
        Save
      </Button>
    </form>
  );
}

function SortableStrategyRow({
  strategy,
  onToggle,
}: {
  strategy: { id: string; enabled: boolean; label: string };
  onToggle: (enabled: boolean) => void;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: strategy.id,
  });

  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      className={
        'flex items-center justify-between rounded-sm px-1 py-1.5 ' +
        (isDragging ? 'z-10 opacity-60' : '')
      }
    >
      <div
        {...attributes}
        {...listeners}
        className="flex items-center gap-2 touch-none"
        aria-label={"Reorder " + strategy.label}
      >
        <GripVertical className="size-3.5 text-fg-muted" />
        <span className="text-[13px]">{strategy.label}</span>
      </div>
      <Switch
        checked={strategy.enabled}
        onCheckedChange={onToggle}
        aria-label={strategy.label}
      />
    </div>
  );
}

function GitHubLoginSection() {
  const queryClient = useQueryClient();
  const [flow, setFlow] = useState<FlowState>({ phase: 'idle' });
  const [copied, setCopied] = useState(false);
  const pendingPollRef = useRef<{ stop: () => void } | null>(null);

  const statusQuery = useQuery({
    queryKey: ['github-oauth-status'],
    queryFn: getGitHubOAuthStatus,
    staleTime: 30_000,
    retry: 1,
  });

  useEffect(() => {
    return () => {
      pendingPollRef.current?.stop();
    };
  }, []);

  const invalidateAfterAuth = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['github-oauth-status'] });
    queryClient.invalidateQueries({ queryKey: ['usage'] });
    queryClient.invalidateQueries({ queryKey: ['system', 'provider-configs'] });
  }, [queryClient]);

  const startPolling = useCallback(
    (deviceCode: string, initialInterval: number) => {
      pendingPollRef.current?.stop();
      let stopped = false;
      let pollMs = initialInterval * 1000;
      let timeoutId: ReturnType<typeof setTimeout>;

      const poll = async () => {
        if (stopped) return;
        try {
          const result = await pollGitHubOAuth(deviceCode);
          if (result.status === 'success') {
            stopped = true;
            setFlow({ phase: 'idle' });
            toast.success('GitHub connected');
            invalidateAfterAuth();
            return;
          }
          if (result.status === 'slow_down' && result.interval) {
            pollMs = result.interval * 1000;
          }
        } catch {
          stopped = true;
          setFlow({ phase: 'error', message: 'Authorisation failed — try again' });
          return;
        }
        if (!stopped) timeoutId = setTimeout(poll, pollMs);
      };

      timeoutId = setTimeout(poll, pollMs);
      pendingPollRef.current = {
        stop: () => {
          stopped = true;
          clearTimeout(timeoutId);
        },
      };
    },
    [invalidateAfterAuth],
  );

  const login = useMutation({
    mutationFn: initGitHubOAuth,
    onSuccess: (data) => {
      const expiresAt = new Date(Date.now() + data.expires_in * 1000).toISOString();
      setFlow({
        phase: 'pending',
        deviceCode: data.device_code,
        userCode: data.user_code,
        verificationUri: data.verification_uri,
        expiresAt,
        pollInterval: data.interval,
      });
      startPolling(data.device_code, data.interval);
    },
    onError: (err) => setFlow({ phase: 'error', message: err.message }),
  });

  const logout = useMutation({
    mutationFn: logoutGitHub,
    onSuccess: () => {
      toast.success('GitHub disconnected');
      invalidateAfterAuth();
    },
    onError: (err) => toast.error(err.message),
  });

  const cancel = () => {
    pendingPollRef.current?.stop();
    setFlow({ phase: 'idle' });
  };

  const copyCode = async (code: string) => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      toast.info(`Code: ${code}`);
    }
  };

  if (flow.phase === 'pending') {
    return (
      <div className="rounded-md border border-edge bg-surface-2 p-4">
        <p className="mb-1 text-[12px] text-fg-subtle">
          Open{' '}
          <a
            href={flow.verificationUri}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-0.5 text-accent underline underline-offset-2"
          >
            github.com/login/device
            <ExternalLink className="size-2.5" />
          </a>{' '}
          and enter this code:
        </p>
        <div className="mb-3 flex items-center gap-2">
          <code className="flex-1 rounded bg-surface-1 px-3 py-2 font-mono text-xl tracking-[0.25em]">
            {flow.userCode}
          </code>
          <Button
            size="icon-sm"
            variant="ghost"
            onClick={() => void copyCode(flow.userCode)}
            title="Copy code"
          >
            {copied ? (
              <span className="text-[10px] font-bold text-ok">✓</span>
            ) : (
              <Copy className="size-3.5" />
            )}
          </Button>
        </div>
        <div className="flex items-center justify-between">
          <Countdown until={flow.expiresAt} prefix="expires in" />
          <div className="flex items-center gap-2">
            <span className="animate-pulse text-[11px] text-fg-muted">Waiting…</span>
            <Button size="sm" variant="ghost" onClick={cancel}>
              Cancel
            </Button>
          </div>
        </div>
      </div>
    );
  }

  if (flow.phase === 'error') {
    return (
      <div className="rounded-md border border-critical/30 bg-critical/5 p-3">
        <p className="mb-2 text-[12px] text-critical">{flow.message}</p>
        <Button size="sm" variant="ghost" onClick={() => setFlow({ phase: 'idle' })}>
          Try again
        </Button>
      </div>
    );
  }

  const auth = statusQuery.data;

  if (auth?.authenticated) {
    return (
      <div className="flex items-center gap-3 rounded-md border border-edge bg-surface-2 px-3 py-2.5">
        <div className="min-w-0 flex-1">
          <p className="text-[13px] font-medium">
            {auth.account ? `@${auth.account}` : 'Connected'}
          </p>
          {auth.email ? (
            <p className="truncate text-[11px] text-fg-subtle">{auth.email}</p>
          ) : null}
        </div>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => logout.mutate()}
          loading={logout.isPending}
          className="shrink-0 text-fg-muted"
        >
          <LogOut className="mr-1 size-3" />
          Disconnect
        </Button>
      </div>
    );
  }

  return (
    <Button
      variant="secondary"
      size="sm"
      className="w-full justify-center"
      onClick={() => login.mutate()}
      loading={login.isPending || statusQuery.isPending}
    >
      <LogIn className="mr-1.5 size-3.5" />
      Connect via GitHub OAuth
    </Button>
  );
}
