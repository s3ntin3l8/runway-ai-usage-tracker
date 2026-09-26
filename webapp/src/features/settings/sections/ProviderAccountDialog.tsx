// Per-account edit form for credentials, labels, poll intervals, and
// collection strategies. It is keyed by `(provider_id, account_id)` and
// opened from `ProviderDetailDialog` for the selected account.

import { useEffect, useMemo, useState } from 'react';
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
import { GripVertical } from 'lucide-react';
import { toast } from 'sonner';
import { putProviderConfig, type ProviderConfigUpdate } from '@/api/endpoints';
import type { CollectionStrategy, ProviderConfig } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { HelperText, Input, Label } from '@/components/ui/Input';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';
import { Switch } from '@/components/ui/Switch';
import { setPullToRefreshSuspended } from '@/lib/pullToRefresh';
import { displayAccountName, accountSubtitle, maskAccountId } from '@/lib/accountDisplay';

interface ProviderAccountDialogProps {
  // The provider envelope — passed for label/help text and supported_strategies
  // defaults. `provider.accounts[i]` is the account being edited.
  provider: ProviderConfig;
  accountId: string;
  onClose: () => void;
  onSaved?: () => void;
}

export function ProviderAccountDialog({
  provider,
  accountId,
  onClose,
  onSaved,
}: ProviderAccountDialogProps) {
  const account = useMemo(
    () => provider.accounts.find((a) => a.account_id === accountId),
    [provider.accounts, accountId],
  );
  const title = account
    ? `Edit account · ${displayAccountName(account)}`
    : `Edit account · ${maskAccountId(accountId)}`;

  if (!account) {
    // Defensive: render an empty-state dialog rather than crashing if the
    // account was deleted between the parent state update and this render.
    return (
      <ResponsiveDialog
        open
        onOpenChange={(open) => {
          if (!open) onClose();
        }}
        title="Account not found"
        description="This account may have been removed."
      >
        <Button variant="secondary" onClick={onClose}>
          Close
        </Button>
      </ResponsiveDialog>
    );
  }

  return (
    <ResponsiveDialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title={title}
      description={
        accountSubtitle(account)
          ? `${accountSubtitle(account)} · stored encrypted on the server`
          : 'Stored encrypted on the server'
      }
    >
      <ProviderAccountForm
        key={account.account_id}
        provider={provider}
        account={account}
        onSaved={() => {
          onSaved?.();
          onClose();
        }}
        onCancel={onClose}
      />
    </ResponsiveDialog>
  );
}

function ProviderAccountForm({
  provider,
  account,
  onSaved,
  onCancel,
}: {
  provider: ProviderConfig;
  account: ProviderConfig['accounts'][number];
  onSaved: () => void;
  onCancel: () => void;
}) {
  const queryClient = useQueryClient();
  const [enabled, setEnabled] = useState(account.enabled);
  const [apiKey, setApiKey] = useState('');
  const [cookie, setCookie] = useState('');
  const [workspaceId, setWorkspaceId] = useState(account.opencode_workspace_id ?? '');
  const [billingType, setBillingType] = useState(account.billing_type ?? 'unknown');
  // PR #287 / #273 — explicit clear flags for the stored credentials. Set
  // by the "Clear" button next to each input. The flag wins over a
  // same-field write, so the user can't accidentally clear a credential
  // they intended to update.
  const [clearApiKey, setClearApiKey] = useState(false);
  const [clearCookie, setClearCookie] = useState(false);
  const [label, setLabel] = useState(account.account_label ?? '');
  const [pollInterval, setPollInterval] = useState(
    account.poll_interval_seconds != null ? String(account.poll_interval_seconds) : '',
  );
  const [strategies, setStrategies] = useState<StrategyEntry[]>(() =>
    initStrategies(provider, account),
  );

  const save = useMutation({
    mutationFn: () => {
      const body: ProviderConfigUpdate = {
        enabled,
        // Empty string means "clear" server-side (mirrors the provider account form's
        // behaviour); trimmed value otherwise.
        account_label: label.trim(),
        poll_interval_seconds: pollInterval.trim() === '' ? null : Number(pollInterval),
        collection_strategies: strategies.map(({ id, enabled: on }) => ({ id, enabled: on })),
        billing_type: billingType,
      };
      if (provider.provider_id === 'opencode') body.opencode_workspace_id = workspaceId.trim();
      if (apiKey !== '') body.api_key = apiKey;
      if (cookie !== '') body.session_cookie = cookie;
      // Clear flags win over same-field writes (PR #287).
      if (clearApiKey) body.clear_api_key = true;
      if (clearCookie) body.clear_session_cookie = true;
      return putProviderConfig(provider.provider_id, account.account_id, body);
    },
    onSuccess: () => {
      toast.success(`${provider.name} · ${displayAccountName(account)} saved`);
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
    const ids = strategies.map((s) => s.id);
    const oldIndex = ids.indexOf(String(active.id));
    const newIndex = ids.indexOf(String(over.id));
    if (oldIndex === -1 || newIndex === -1) return;
    setStrategies(arrayMove(strategies, oldIndex, newIndex));
  };

  // Safety net: dnd-kit normally clears the suspend flag on onDragEnd /
  // onDragCancel, but a mid-drag unmount skips both — prevent permanently
  // stuck pull-to-refresh.
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
        <Label htmlFor="acct-enabled">Collection enabled</Label>
        <Switch id="acct-enabled" checked={enabled} onCheckedChange={setEnabled} />
      </div>

      <div className="flex flex-col gap-1.5">
        <Label htmlFor="acct-billing-type">Billing type</Label>
        <select
          id="acct-billing-type"
          value={billingType}
          onChange={(event) => setBillingType(event.target.value as typeof billingType)}
          className="h-9 rounded-md border border-border bg-surface-1 px-3 text-sm text-fg"
        >
          <option value="unknown">Unknown · show usage value</option>
          <option value="subscription">Subscription · show estimated usage value</option>
          <option value="pay_as_you_go">Pay as you go · show reported cost when available</option>
        </select>
        <HelperText>
          Estimated usage value uses configured API rates. Reported cost comes from the provider log.
        </HelperText>
      </div>

      {provider.provider_id === 'github' && account.account_id === 'default' ? (
        <div className="flex flex-col gap-1.5">
          <Label>GitHub login</Label>
          <p className="text-[12px] text-fg-muted">
            GitHub OAuth currently operates on the canonical account. Multi-account
            GitHub is tracked in #289.
          </p>
        </div>
      ) : null}

      {provider.supports_api_key ? (
        <div className="flex flex-col gap-1.5">
          <div className="flex items-center justify-between">
            <Label htmlFor="acct-key">{provider.api_key_label || 'API key'}</Label>
            {account.api_key_set && !clearApiKey ? (
              <Button
                type="button"
                variant="danger-ghost"
                size="sm"
                onClick={() => {
                  setClearApiKey(true);
                  setApiKey('');
                }}
              >
                Clear
              </Button>
            ) : null}
          </div>
          <Input
            id="acct-key"
            type="password"
            autoComplete="off"
            value={apiKey}
            onChange={(e) => {
              setApiKey(e.target.value);
              if (clearApiKey) setClearApiKey(false);
            }}
            placeholder={
              clearApiKey
                ? 'Will be cleared on save'
                : account.api_key_set
                  ? '••••••••  (set — leave blank to keep)'
                  : ''
            }
            disabled={clearApiKey}
          />
          {provider.api_key_help ? <HelperText>{provider.api_key_help}</HelperText> : null}
        </div>
      ) : null}

      {provider.supports_session_cookie ? (
        <div className="flex flex-col gap-1.5">
          <div className="flex items-center justify-between">
            <Label htmlFor="acct-cookie">
              {provider.session_cookie_label || 'Session cookie'}
            </Label>
            {account.session_cookie_set && !clearCookie ? (
              <Button
                type="button"
                variant="danger-ghost"
                size="sm"
                onClick={() => {
                  setClearCookie(true);
                  setCookie('');
                }}
              >
                Clear
              </Button>
            ) : null}
          </div>
          <Input
            id="acct-cookie"
            type="password"
            autoComplete="off"
            value={cookie}
            onChange={(e) => {
              setCookie(e.target.value);
              if (clearCookie) setClearCookie(false);
            }}
            placeholder={
              clearCookie
                ? 'Will be cleared on save'
                : account.session_cookie_set
                  ? '••••••••  (set — leave blank to keep)'
                  : ''
            }
            disabled={clearCookie}
          />
          {provider.session_cookie_help ? (
            <HelperText>{provider.session_cookie_help}</HelperText>
          ) : null}
        </div>
      ) : null}

      {provider.provider_id === 'opencode' ? (
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="opencode-workspace-id">OpenCode workspace ID</Label>
          <Input
            id="opencode-workspace-id"
            autoComplete="off"
            value={workspaceId}
            onChange={(e) => setWorkspaceId(e.target.value)}
            placeholder="Required when this account has multiple Go workspaces"
          />
          <HelperText>Find the ID in your OpenCode Console workspace settings.</HelperText>
        </div>
      ) : null}

      <div className="grid grid-cols-2 gap-3">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="acct-label">Account label</Label>
          <Input
            id="acct-label"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder={maskAccountId(account.account_id)}
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="acct-poll">Poll interval (s)</Label>
          <Input
            id="acct-poll"
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
          <legend className="px-1 text-xs font-medium text-fg-muted">
            Collection strategies
          </legend>
          <DndContext
            sensors={sensors}
            collisionDetection={closestCenter}
            onDragStart={() => setPullToRefreshSuspended(true)}
            onDragEnd={handleDragEnd}
            onDragCancel={() => setPullToRefreshSuspended(false)}
          >
            <SortableContext
              items={strategies.map((s) => s.id)}
              strategy={verticalListSortingStrategy}
            >
              {strategies.map((s) => (
                <SortableStrategyRow
                  key={s.id}
                  strategy={s}
                  onToggle={(on) =>
                    setStrategies((prev) =>
                      prev.map((x) => (x.id === s.id ? { ...x, enabled: on } : x)),
                    )
                  }
                />
              ))}
            </SortableContext>
          </DndContext>
        </fieldset>
      ) : null}

      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onCancel} type="button">
          Cancel
        </Button>
        <Button type="submit" variant="primary" loading={save.isPending}>
          Save
        </Button>
      </div>
    </form>
  );
}

interface StrategyEntry {
  id: string;
  enabled: boolean;
  label: string;
}

/**
 * Pick the strategies for the form:
 *   - If the row has explicit `collection_strategies`, prefer those (preserves
 *     user-set overrides).
 *   - Otherwise fall back to `provider.supported_strategies` (registry defaults).
 */
function initStrategies(provider: ProviderConfig, account: ProviderConfig['accounts'][number]): StrategyEntry[] {
  const src: CollectionStrategy[] =
    account.collection_strategies ?? provider.supported_strategies ?? [];
  return src.map((s) => ({
    id: s.id,
    enabled: s.enabled,
    label: (s as { label?: string }).label ?? s.id,
  }));
}

function SortableStrategyRow({
  strategy,
  onToggle,
}: {
  strategy: StrategyEntry;
  onToggle: (enabled: boolean) => void;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: strategy.id,
  });

  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      className={`flex items-center justify-between rounded-sm px-1 py-1.5 ${
        isDragging ? 'z-10 opacity-60' : ''
      }`}
    >
      <div
        {...attributes}
        {...listeners}
        className="flex items-center gap-2 touch-none"
        aria-label={'Reorder ' + strategy.label}
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
