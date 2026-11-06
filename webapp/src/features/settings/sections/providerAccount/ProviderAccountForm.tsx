// The account edit form. State and save logic stay here; the dialog shell
// only opens and closes it.

import { useEffect, useState } from 'react';
import { useMutation } from '@tanstack/react-query';
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
  verticalListSortingStrategy,
} from '@dnd-kit/sortable';
import { toast } from 'sonner';
import {
  patchCredentialSources,
  putProviderConfig,
  type ProviderConfigUpdate,
} from '@/api/endpoints';
import type { BillingType, CredentialSourceSummary, ProviderConfig } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { HelperText, Input, Label } from '@/components/ui/Input';
import { Switch } from '@/components/ui/Switch';
import { useInvalidateCredentialViews } from '@/hooks/useInvalidateCredentialViews';
import { setPullToRefreshSuspended } from '@/lib/pullToRefresh';
import { displayAccountName, maskAccountId } from '@/lib/accountDisplay';
import { BILLING_TYPE_LABELS } from '@/lib/billingType';
import { GitHubLoginSection } from '../GitHubLoginSection';
import { CredentialSourcesEditor } from './CredentialSourcesEditor';
import { initStrategies, type StrategyEntry } from './initStrategies';
import { SortableStrategyRow } from './SortableStrategyRow';

export function ProviderAccountForm({
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
  const [credentialSources, setCredentialSources] = useState<CredentialSourceSummary[]>(() =>
    [...(account.credential_sources ?? [])].sort((a, b) => a.priority - b.priority),
  );
  const [allMachines, setAllMachines] = useState(false);
  // Saving an unrelated field (label, poll interval, ...) must not rewrite the source
  // order — that PATCH is audited and can fan out to every machine.
  const sourceFingerprint = (list: CredentialSourceSummary[]) =>
    JSON.stringify(list.map((source, priority) => [source.source_id, source.enabled, priority]));
  const sourcesChanged =
    sourceFingerprint(credentialSources) !==
    sourceFingerprint(
      [...(account.credential_sources ?? [])].sort((a, b) => a.priority - b.priority),
    );
  const invalidateCredentialViews = useInvalidateCredentialViews();

  const save = useMutation({
    mutationFn: async () => {
      if (credentialSources.length > 0 && (sourcesChanged || allMachines)) {
        await patchCredentialSources(
          provider.provider_id,
          account.account_id,
          // Send the complete source set; array position is the zero-based rank.
          credentialSources.map((source, priority) => ({
            source_id: source.source_id,
            enabled: source.enabled,
            priority,
          })),
          allMachines,
        );
      }
      if (account.source === 'discovered') return { status: 'saved' };
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
      // A save can change which credentials feed which account (source order, a pasted
      // key), so refresh every credential view, not just the provider list.
      invalidateCredentialViews();
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
      <CredentialSourcesEditor
        sources={credentialSources}
        onChange={setCredentialSources}
        allMachines={allMachines}
        onAllMachinesChange={setAllMachines}
      />

      {account.source === 'discovered' ? null : <>
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
          {(Object.keys(BILLING_TYPE_LABELS) as BillingType[]).map((type) => (
            <option key={type} value={type}>{BILLING_TYPE_LABELS[type]}</option>
          ))}
        </select>
        <HelperText>
          Estimated cost uses configured API rates. Reported cost comes from the provider log.
        </HelperText>
      </div>

      {provider.provider_id === 'github' ? (
        <div className="flex flex-col gap-1.5">
          <Label>Sign in with GitHub</Label>
          <GitHubLoginSection />
          <HelperText>
            Uses GitHub&apos;s device flow; Runway stores the token encrypted on the server.
          </HelperText>
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
      </>}

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
