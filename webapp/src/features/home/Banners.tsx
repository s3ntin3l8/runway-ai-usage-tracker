// Inline attention banners: expiring/expired credentials and usage
// anomalies. Dismissals are session-local (the conditions re-evaluate on
// every poll anyway).

import { useState } from 'react';
import { Link } from 'react-router';
import { KeyRound, TrendingUp, X } from 'lucide-react';
import type { AnomalyEntry, TokenHealthEntry } from '@/api/types';
import { cn } from '@/lib/cn';

interface BannersProps {
  tokens: TokenHealthEntry[] | undefined;
  anomalies: AnomalyEntry[] | undefined;
}

export function Banners({ tokens, anomalies }: BannersProps) {
  const unhealthy = (tokens ?? []).filter(
    (t) => t.status === 'expired' || t.status === 'expiring',
  );
  const spikes = anomalies ?? [];

  return (
    <>
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
