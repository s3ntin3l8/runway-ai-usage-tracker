// Inline attention banners: collection failures, expiring/expired
// credentials, and usage anomalies. Dismissals are session-local (the
// conditions re-evaluate on every poll anyway).

import { useState } from 'react';
import { Link } from 'react-router';
import { AlertTriangle, KeyRound, TrendingUp, X } from 'lucide-react';
import type { AnomalyEntry, FleetEntry, TokenHealthEntry } from '@/api/types';
import { timeAgo } from '@/lib/format';
import { cn } from '@/lib/cn';

interface BannersProps {
  tokens: TokenHealthEntry[] | undefined;
  anomalies: AnomalyEntry[] | undefined;
  fleet?: FleetEntry[] | undefined;
}

function isCollectionFailing(card: { detail?: string | null; stale?: boolean }): boolean {
  return /collection failing/i.test(card.detail ?? '') || card.stale === true;
}

export function Banners({ tokens, anomalies, fleet }: BannersProps) {
  const unhealthy = (tokens ?? []).filter(
    (t) => (t.status === 'expired' || t.status === 'expiring') && !t.redundant,
  );
  const spikes = anomalies ?? [];
  const failing = (fleet ?? []).filter((e) => {
    const cards = [e.critical_gauge, ...(e.secondary_limits ?? [])];
    return cards.some((c) => isCollectionFailing(c ?? {}));
  });

  const failingLabel = (e: FleetEntry): string => {
    const gauge = e.critical_gauge;
    const name = (gauge?.service_name || e.provider_id) as string;
    const when = gauge?.fetched_at || gauge?.updated_at;
    return when ? `${name} (last ok ${timeAgo(when)})` : name;
  };

  return (
    <>
      {failing.length > 0 ? (
        <Banner tone="critical" icon={<AlertTriangle className="size-4 shrink-0" aria-hidden />}>
          <span>
            {failing.length === 1
              ? `Collection failing for ${failingLabel(failing[0])}.`
              : `Collection failing for ${failing.length} providers: ${failing
                  .slice(0, 3)
                  .map(failingLabel)
                  .join(', ')}${failing.length > 3 ? ` and ${failing.length - 3} more` : ''}.`}{' '}
            <Link to="/settings" className="font-medium underline underline-offset-2">
              Check settings
            </Link>
          </span>
        </Banner>
      ) : null}
      {unhealthy.length > 0 ? (
        <Banner tone="critical" icon={<KeyRound className="size-4 shrink-0" aria-hidden />}>
          <span>
            {unhealthy.length === 1
              ? `Credential for ${unhealthy[0].provider} (${unhealthy[0].account_label || unhealthy[0].account_id}) is ${unhealthy[0].status}.`
              : `${unhealthy.length} credentials are expiring or expired.`}{' '}
            <Link to="/settings/tokens" className="font-medium underline underline-offset-2">
              Review tokens
            </Link>
          </span>
        </Banner>
      ) : null}
      {spikes.length > 0 ? (
        <Banner tone="warning" icon={<TrendingUp className="size-4 shrink-0" aria-hidden />}>
          <span>
            Unusual usage today:{' '}
            {spikes
              .slice(0, 2)
              .map((a) => `${a.provider_id}/${a.model_id} (${a.z_score_tokens.toFixed(1)}σ)`)
              .join(', ')}
            {spikes.length > 2 ? ` and ${spikes.length - 2} more` : ''}
          </span>
        </Banner>
      ) : null}
    </>
  );
}

function Banner({
  tone,
  icon,
  children,
}: {
  tone: 'critical' | 'warning';
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  const [dismissed, setDismissed] = useState(false);
  if (dismissed) return null;
  return (
    <div
      role="status"
      className={cn(
        'flex items-center gap-2.5 rounded-md border px-4 py-2.5 text-[13px]',
        tone === 'critical'
          ? 'border-critical/30 bg-critical-muted text-critical'
          : 'border-warning/30 bg-warning-muted text-warning',
      )}
    >
      {icon}
      <div className="flex-1">{children}</div>
      <button
        type="button"
        aria-label="Dismiss"
        onClick={() => setDismissed(true)}
        className="-m-1 cursor-pointer rounded-sm p-1 opacity-70 transition-opacity duration-150 hover:opacity-100"
      >
        <X className="size-3.5" />
      </button>
    </div>
  );
}
