// Inline attention banners: collection failures, expiring/expired
// credentials, and usage anomalies. A dismissal is remembered in localStorage
// against a fingerprint of the banner's condition: it stays hidden across reloads,
// restarts and updates, and comes back only when the condition changes.

import { useState } from 'react';
import { Link } from 'react-router';
import { AlertTriangle, BellOff, HeartPulse, KeyRound, TrendingUp, Unlink, X } from 'lucide-react';
import type { AnomalyEntry, CredentialInventory, DataHealthReport, FleetEntry } from '@/api/types';
import { timeAgo } from '@/lib/format';
import { cardStale } from '@/lib/quota';
import { cn } from '@/lib/cn';
import { credentialsNeedingAttention } from '@/lib/credentialAttention';

const DISMISS_PREFIX = 'runway:banner-dismissed:';

const fingerprintOf = (parts: string[]): string => [...parts].sort().join('|');

function readDismissed(kind: string): string | null {
  try {
    return localStorage.getItem(DISMISS_PREFIX + kind);
  } catch {
    return null;
  }
}

interface BannersProps {
  credentials: CredentialInventory | undefined;
  anomalies: AnomalyEntry[] | undefined;
  fleet?: FleetEntry[] | undefined;
  dataHealth?: DataHealthReport | undefined;
}

export function Banners({ credentials, anomalies, fleet, dataHealth }: BannersProps) {
  const unhealthy = credentialsNeedingAttention(credentials);
  const blocked = credentials?.blocked_collection ?? [];
  const machineNames = new Map((credentials?.machines ?? []).map((m) => [m.machine_id, m.name]));
  const spikes = anomalies ?? [];
  const failing = (fleet ?? []).filter((e) => {
    const cards = [e.critical_gauge, ...(e.secondary_limits ?? [])];
    return cards.some((c) => c != null && cardStale(c));
  });
  const dataHealthErrors = (dataHealth?.checks ?? []).filter(
    (c) => c.severity === 'error' && c.total_count > 0,
  );
  // Not a repair, so it has its own banner (and its own link) instead of the generic one.
  const noAlertChannel = (dataHealth?.checks ?? []).some(
    (c) => c.check_id === 'alert_channels' && c.total_count > 0,
  );

  // Anomalies are per-day, so the date is part of the fingerprint: tomorrow's spike re-shows.
  const today = new Date().toLocaleDateString('en-CA');
  const fp = {
    collection: fingerprintOf(failing.map((e) => `${e.provider_id}/${e.account_id}`)),
    unmapped: fingerprintOf(
      blocked.map((b) => `${b.sidecar_id}/${b.provider_id}/${b.credential_origin}`),
    ),
    credentials: fingerprintOf(
      unhealthy.map((c) => `${c.provider}/${c.accountName}/${c.status}`),
    ),
    anomalies: `${today}:${fingerprintOf(
      spikes.map((a) => `${a.provider_id}/${a.account_id}/${a.model_id}`),
    )}`,
    alertChannel: 'no-channel',
    dataHealth: fingerprintOf(dataHealthErrors.map((c) => c.check_id)),
  };

  const failingLabel = (e: FleetEntry): string => {
    const staleCard = [e.critical_gauge, ...(e.secondary_limits ?? [])].find(
      (card) => card != null && cardStale(card),
    );
    const name = (staleCard?.service_name || e.provider_id) as string;
    const when = staleCard?.fetched_at || staleCard?.updated_at;
    return when ? `${name} (last ok ${timeAgo(when)})` : name;
  };

  return (
    <>
      {failing.length > 0 ? (
        <Banner
          tone="critical"
          icon={<AlertTriangle className="size-4 shrink-0" aria-hidden />}
          kind="collection"
          fingerprint={fp.collection}
        >
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
      {blocked.length > 0 ? (
        <Banner
          tone="critical"
          icon={<Unlink className="size-4 shrink-0" aria-hidden />}
          kind="unmapped"
          fingerprint={fp.unmapped}
        >
          <span>
            {blocked.length === 1
              ? `Credential unmapped on ${
                  machineNames.get(blocked[0].sidecar_id) ?? blocked[0].sidecar_id
                } — quota for ${blocked[0].provider_id} won't collect until it is assigned an account.`
              : `${blocked.length} credentials are unmapped — quota for ${[
                  ...new Set(blocked.map((b) => b.provider_id)),
                ].join(', ')} won't collect until they are assigned an account.`}{' '}
            <Link
              to={
                blocked.length === 1
                  ? `/settings/credentials?${new URLSearchParams({
                      view: 'mapping',
                      sidecar: blocked[0].sidecar_id,
                      provider: blocked[0].provider_id,
                      // Without a credential fingerprint ("#…"): it adds nothing to find the
                      // row and doesn't belong in history or logs.
                      origin: blocked[0].credential_origin.split('#')[0],
                    }).toString()}`
                  : '/settings/credentials?view=mapping'
              }
              className="font-medium underline underline-offset-2"
            >
              Assign account
            </Link>
          </span>
        </Banner>
      ) : null}
      {unhealthy.length > 0 ? (
        <Banner
          tone="critical"
          icon={<KeyRound className="size-4 shrink-0" aria-hidden />}
          kind="credentials"
          fingerprint={fp.credentials}
        >
          <span>
            {unhealthy.length === 1
              ? `Credential for ${unhealthy[0].provider} (${unhealthy[0].accountName}) ${
                  unhealthy[0].status === 'invalid'
                    ? 'was rejected by the provider'
                    : unhealthy[0].status === 'failing'
                      ? 'keeps failing to collect'
                      : `is ${unhealthy[0].status}`
                }.`
              : `${unhealthy.length} credentials need attention (expiring, expired, rejected or failing).`}{' '}
            <Link to="/settings/credentials" className="font-medium underline underline-offset-2">
              Review credentials
            </Link>
          </span>
        </Banner>
      ) : null}
      {spikes.length > 0 ? (
        <Banner
          tone="warning"
          icon={<TrendingUp className="size-4 shrink-0" aria-hidden />}
          kind="anomalies"
          fingerprint={fp.anomalies}
        >
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
      {noAlertChannel ? (
        <Banner
          tone="warning"
          icon={<BellOff className="size-4 shrink-0" aria-hidden />}
          kind="alert-channel"
          fingerprint={fp.alertChannel}
        >
          <span>
            Credential alerts have no delivery channel — an expired or rejected credential won't
            notify anyone.{' '}
            <Link to="/settings/webhooks" className="font-medium underline underline-offset-2">
              Add a webhook
            </Link>
          </span>
        </Banner>
      ) : null}
      {dataHealthErrors.length > 0 ? (
        <Banner
          tone="warning"
          icon={<HeartPulse className="size-4 shrink-0" aria-hidden />}
          kind="data-health"
          fingerprint={fp.dataHealth}
        >
          <span>
            {dataHealthErrors.length === 1
              ? `Data health found an issue: ${dataHealthErrors[0].title}.`
              : `Data health found issues in ${dataHealthErrors.length} checks.`}{' '}
            <Link
              to="/settings/data-health"
              className="font-medium underline underline-offset-2"
            >
              Review and fix
            </Link>
          </span>
        </Banner>
      ) : null}
    </>
  );
}

function Banner({
  tone,
  icon,
  kind,
  fingerprint,
  children,
}: {
  tone: 'critical' | 'warning';
  icon: React.ReactNode;
  kind: string;
  fingerprint: string;
  children: React.ReactNode;
}) {
  // Session fallback for when localStorage is unavailable (private mode / quota); also
  // re-renders after a dismiss so the stored fingerprint is re-read.
  const [sessionDismissed, setSessionDismissed] = useState<string | null>(null);
  if (readDismissed(kind) === fingerprint || sessionDismissed === fingerprint) return null;

  const dismiss = () => {
    try {
      localStorage.setItem(DISMISS_PREFIX + kind, fingerprint);
    } catch {
      // Fall through to the session-local fallback below.
    }
    setSessionDismissed(fingerprint);
  };
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
        onClick={dismiss}
        className="-m-1 cursor-pointer rounded-sm p-1 opacity-70 transition-opacity duration-150 hover:opacity-100"
      >
        <X className="size-3.5" />
      </button>
    </div>
  );
}
