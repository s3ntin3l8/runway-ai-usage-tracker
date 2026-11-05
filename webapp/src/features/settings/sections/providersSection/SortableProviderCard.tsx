import { useSortable } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import type { ProviderConfig } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Card } from '@/components/ui/Card';
import { ProviderGlyph } from '@/components/ui/ProviderGlyph';

export function SortableProviderCard({
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
