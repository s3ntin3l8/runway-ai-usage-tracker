// One pending credential inside the untagged-credentials dialog: its origin,
// the account it maps to, and whether the tag applies to this machine only.

import { Link } from 'react-router';
import { TriangleAlert } from 'lucide-react';

import type { ProviderAccount, UntaggedCredential } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { Label } from '@/components/ui/Input';
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select';
import { maskAccountId } from '@/lib/accountDisplay';
import { formatLocalDateTime } from '@/lib/tz';
import { ClaudeSourceOrganizationPicker } from './ClaudeSourceOrganizationPicker';
import {
  accountPickNotice,
  archivedAccountNotice,
  displayOrigin,
  isMachineBoundOrigin,
  sourceDescription,
  sourceLabel,
  type DialogState,
} from './untaggedCredentialModel';

interface UntaggedRowProps {
  entry: UntaggedCredential;
  /** Display name of the reporting machine; falls back to the raw sidecar id. */
  machineName?: string;
  accounts: ProviderAccount[];
  currentSelection: DialogState;
  onSelect: (accountId: string) => void;
  onScope: (allMachines: boolean) => void;
  onSave: () => void;
  saving: boolean;
  onOrganizationComplete: () => void;
  /** Called when a link leaves the dialog (it is opened from Provider Settings itself,
   *  so a same-route link would otherwise leave it open and look dead). */
  onNavigate: () => void;
}

export function UntaggedRow({
  entry,
  machineName,
  accounts,
  currentSelection,
  onSelect,
  onScope,
  onSave,
  saving,
  onOrganizationComplete,
  onNavigate,
}: UntaggedRowProps) {
  // Each row tracks its own Select state via currentSelection.account_id
  // when the entry matches. We keep that gating intentionally simple —
  // one staged selection at a time per dialog open.
  const isActive =
    currentSelection.provider_id === entry.provider_id &&
    currentSelection.credential_origin === entry.credential_origin &&
    currentSelection.sidecar_id === entry.sidecar_id;

  const selectedAccountId = isActive ? currentSelection.account_id : '';
  // Browser cookies and keychain entries belong to one machine, so an "All machines" tag
  // would follow an account switch on a host it was never made for (the server refuses it).
  const machineBound = isMachineBoundOrigin(entry.credential_origin);
  const allMachines = isActive && currentSelection.all_machines && !machineBound;
  // A key-scoped origin carries a `#fingerprint`; a plain path/env origin is shared as-is.
  const isShared = allMachines && !entry.credential_origin.includes('#');

  // The accounts this credential can be mapped to, in two groups:
  //
  // - active: enabled and not archived — the normal case.
  // - archived: the operator retired the account. Mapping here *parks* the
  //   credential: the binding is recorded (the origin stops being pending and
  //   the operator is not nagged) but nothing is collected until the account
  //   is restored, which revives the binding automatically.
  //
  // Disabled-but-not-archived rows stay unpickable: tagging to one stores
  // a hint the server won't collect (the sidecar's
  // ``accounts`` view in /fleet/config only ships enabled rows).
  // PR #290 round-2 review (Hermes body suggestion #4).
  const activeAccounts = accounts.filter((a) => !a.archived && a.enabled !== false);
  const archivedAccounts = accounts.filter((a) => a.archived);
  const disabledAccounts = accounts.filter((a) => !a.archived && a.enabled === false);
  const pickableAccounts = [...activeAccounts, ...archivedAccounts];
  // The copy shown when there is no account to map to at all.
  const notice = accountPickNotice(disabledAccounts.length);
  // The copy shown when the only options are retired accounts.
  const parkedNotice = archivedAccountNotice(archivedAccounts.length);

  return (
    <Card
      className="p-3"
      role="group"
      aria-label={`${sourceLabel(entry)} on ${machineName ?? entry.sidecar_id}`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[13px] font-semibold">
            {entry.provider_id}
            {sourceDescription(entry).label ? (
              <span className="text-[11px] font-normal text-fg-muted">
                {' '}
                {sourceLabel(entry)}
              </span>
            ) : null}
          </p>
          <p className="truncate text-[11px] text-fg-subtle">
            <span className="font-mono">{displayOrigin(entry.credential_origin)}</span>
            {' · from '}
            <span className="font-mono">{machineName ?? entry.sidecar_id}</span>
            {allMachines && <span className="text-accent-text"> · applies to all machines</span>}
          </p>
          {sourceDescription(entry).kind === 'cookie' ? (
            <p className="text-[11px] text-fg-subtle">
              A browser session cookie the machine scraped — not a credential file.
            </p>
          ) : null}
          {entry.claimed_account_id && (
            <p className="text-[11px] text-fg-subtle">
              Discovered login: <span className="font-mono">{entry.claimed_account_id}</span>
            </p>
          )}
        </div>
      </div>

      {entry.quota_preview_stale ? (
        <p className="mt-2 rounded-sm bg-surface-2 px-2.5 py-2 text-[11px] text-fg-subtle" role="status">
          The quota preview expired. A fresh preview will appear after the next successful check.
          {entry.quota_preview_observed_at ? (
            <> Last observed {formatLocalDateTime(entry.quota_preview_observed_at)}.</>
          ) : null}
        </p>
      ) : null}

      {entry.quota_preview?.length && !entry.quota_preview_stale ? (
        <div className="mt-2 rounded-sm bg-surface-2 px-2.5 py-2 text-[11px]">
          <p className="font-medium">Live quota from this credential</p>
          {entry.quota_preview_observed_at ? (
            <p className="text-fg-subtle">
              Observed {formatLocalDateTime(entry.quota_preview_observed_at)}
            </p>
          ) : null}
          <ul className="mt-1 flex flex-col gap-0.5 text-fg-subtle">
            {entry.quota_preview.map((quota, index) => (
              <li key={`${quota.service_name ?? 'quota'}-${quota.window_type ?? index}`}>
                {quota.service_name ?? entry.provider_id}
                {quota.window_type ? ` · ${quota.window_type}` : ''}
                {quota.remaining !== undefined
                  ? ` · ${quota.remaining}${quota.unit ? ` ${quota.unit}` : ''}`
                  : ''}
                {quota.pct_used !== undefined ? ` · ${quota.pct_used}% used` : ''}
              </li>
            ))}
          </ul>
          <p className="mt-1 text-fg-subtle">Not added to account history until assigned.</p>
        </div>
      ) : null}

      {isClaudeBrowserSource(entry) ? (
        <ClaudeSourceOrganizationPicker sourceId={entry.source_id!} providerId={entry.provider_id} onComplete={onOrganizationComplete} />
      ) : pickableAccounts.length === 0 ? (
        <p className="mt-2 flex items-start gap-2 text-[12px] text-warning">
          <TriangleAlert className="mt-px size-3.5 shrink-0" aria-hidden />
          {/* The whole message lives in one span: as flex children of this <p>, the
              text runs, the provider id and the link would each become their own
              flex item and wrap independently (ragged, gappy layout). One span
              keeps it a single flowing sentence next to the icon. */}
          <span className="min-w-0">
            {notice.lead}{' '}
            <span className="font-mono">{entry.provider_id}</span>{' '}
            {notice.tail}{' '}
            <Link
              to="/settings/providers"
              onClick={onNavigate}
              className="text-accent-text underline underline-offset-2"
            >
              {notice.linkLabel}
            </Link>
            .
          </span>
        </p>
      ) : (
        <div className="mt-3 flex items-end gap-2">
          <div className="min-w-0 flex-1">
            <Label htmlFor={`tag-${entry.sidecar_id}-${entry.credential_origin}`}>Map to</Label>
            <Select
              value={selectedAccountId}
              onValueChange={onSelect}
            >
              <SelectTrigger
                id={`tag-${entry.sidecar_id}-${entry.credential_origin}`}
                className="w-full"
                label="Account to map to"
              >
                <SelectValue placeholder="Pick an account…" />
              </SelectTrigger>
              <SelectContent>
                {activeAccounts.map((a) => (
                  <SelectItem
                    key={`${entry.provider_id}/${a.account_id}`}
                    value={a.account_id}
                  >
                    {a.account_label || maskAccountId(a.account_id)}
                    {a.account_label ? ` · ${maskAccountId(a.account_id)}` : ''}
                  </SelectItem>
                ))}
                {archivedAccounts.length > 0 ? (
                  <SelectGroup>
                    <SelectLabel>Archived · parked until restored</SelectLabel>
                    {archivedAccounts.map((a) => (
                      <SelectItem
                        key={`${entry.provider_id}/${a.account_id}`}
                        value={a.account_id}
                      >
                        {a.account_label || maskAccountId(a.account_id)}
                        {a.account_label ? ` · ${maskAccountId(a.account_id)}` : ''}
                        {' · archived'}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                ) : null}
              </SelectContent>
            </Select>
          </div>
          <Button
            variant="primary"
            onClick={onSave}
            disabled={!selectedAccountId}
            loading={saving}
          >
            Tag
          </Button>
        </div>
      )}

      {activeAccounts.length === 0 && archivedAccounts.length > 0 ? (
        <p className="mt-2 text-[12px] text-fg-subtle">
          {parkedNotice.lead}{' '}
          <span className="font-mono">{entry.provider_id}</span>{' '}
          {parkedNotice.tail}{' '}
          <Link
            to="/settings/providers"
            onClick={onNavigate}
            className="text-accent-text underline underline-offset-2"
          >
            {parkedNotice.linkLabel}
          </Link>
          .
        </p>
      ) : null}

      {pickableAccounts.length > 0 && !isClaudeBrowserSource(entry) ? (
        <div className="mt-2">
          <div className="flex items-center justify-between gap-3">
            <p className="text-[11px] text-fg-subtle">
              {machineBound
                ? 'Browser and keychain credentials belong to one machine, so the tag applies only to the sidecar that reported it.'
                : allMachines
                  ? 'Applies to every sidecar that reports this credential.'
                  : 'Applies only to the sidecar that reported it.'}
            </p>
            <div
              className="flex shrink-0 rounded-md border border-border bg-surface-1 p-0.5"
              role="radiogroup"
              aria-label={`Tag scope for ${displayOrigin(entry.credential_origin)} on ${machineName ?? entry.sidecar_id}`}
            >
              <button
                type="button"
                role="radio"
                aria-checked={!allMachines}
                className={`rounded px-2 py-1 text-[11px] font-medium transition-colors ${
                  !allMachines ? 'bg-accent text-accent-fg' : 'text-fg-muted hover:text-fg'
                }`}
                onClick={() => onScope(false)}
              >
                This machine
              </button>
              <button
                type="button"
                role="radio"
                aria-checked={allMachines}
                disabled={machineBound}
                title={machineBound ? 'Not available for browser or keychain credentials' : undefined}
                className={`rounded px-2 py-1 text-[11px] font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                  allMachines ? 'bg-accent text-accent-fg' : 'text-fg-muted hover:text-fg'
                }`}
                onClick={() => onScope(true)}
              >
                All machines
              </button>
            </div>
          </div>
          {isShared ? (
            <p className="mt-1 text-[11px] text-warning">
              This rule would apply on every machine and isn&apos;t tied to the credential itself —
              if the account behind it changes, data keeps landing on the old one.
            </p>
          ) : null}
        </div>
      ) : null}
    </Card>
  );
}

function isClaudeBrowserSource(entry: UntaggedCredential): boolean {
  return Boolean(entry.source_id) && entry.credential_origin.startsWith('cookie:') && ['anthropic', 'anthropic_console'].includes(entry.provider_id);
}
