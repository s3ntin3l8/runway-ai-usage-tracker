// Hero of the risk-first home: providers most likely to hit a limit,
// most urgent first. Collapses to a slim all-clear row when nothing burns.

import { useNavigate } from 'react-router';
import { CircleCheck, TrendingUp } from 'lucide-react';
import { Badge } from '@/components/ui/Badge';
import { Card } from '@/components/ui/Card';
import { Countdown } from '@/components/ui/Countdown';
import { Gauge } from '@/components/ui/Gauge';
import { ProviderGlyph } from '@/components/ui/ProviderGlyph';
import { formatPct } from '@/lib/format';
import { cardPct, windowLabel } from '@/lib/quota';
import { forecastLabel, type RiskItem } from './risk';

interface AtRiskRailProps {
  items: RiskItem[]; // pre-filtered + sorted (atRiskItems)
  providerNames: Map<string, string>;
}

export function AtRiskRail({ items, providerNames }: AtRiskRailProps) {
  if (items.length === 0) {
    return (
      <div className="flex items-center gap-2.5 rounded-md border border-edge bg-surface-1 px-4 py-3">
        <CircleCheck className="size-4 text-ok" aria-hidden />
        <p className="text-[13px] text-fg-muted">
          All clear — no provider is close to a limit right now.
        </p>
      </div>
    );
  }

  return (
    <section aria-label="At-risk providers" className="flex flex-col gap-2">
      <h2 className="text-xs font-semibold tracking-wide text-fg-subtle uppercase">At risk</h2>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {items.map((item) => (
          <AtRiskCard key={item.key} item={item} providerNames={providerNames} />
        ))}
      </div>
    </section>
  );
}

function AtRiskCard({ item, providerNames }: { item: RiskItem; providerNames: Map<string, string> }) {
  const navigate = useNavigate();
  const { entry, forecast } = item;
  // Use the lead window — the window with the worst combined gauge+forecast
  // severity — so the %, label, gauge colour, and countdown all describe the
  // same window as the "runs out" badge.
  const { card: gauge, status } = item.lead;
  const name = providerNames.get(entry.provider_id) ?? entry.provider_id;
  const fLabel = forecastLabel(forecast);

  return (
    <Card
      role="button"
      tabIndex={0}
      onClick={() => navigate(providerPath(entry.provider_id, entry.account_id))}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          navigate(providerPath(entry.provider_id, entry.account_id));
        }
      }}
      className="cursor-pointer p-4 transition-colors duration-150 hover:border-edge-strong active:scale-[0.99]"
    >
      <div className="flex items-center gap-2.5">
        <ProviderGlyph providerId={entry.provider_id} name={name} />
        <div className="min-w-0 flex-1">
          <p className="truncate text-[13px] font-semibold">{name}</p>
          <p className="truncate text-[11px] text-fg-subtle">
            {gauge.account_label || entry.account_id}
          </p>
        </div>
        {fLabel ? (
          <Badge variant={item.level === 'critical' ? 'critical' : 'warning'}>
            <TrendingUp className="size-3" aria-hidden /> {fLabel}
          </Badge>
        ) : null}
      </div>
      <div className="mt-4 flex items-baseline justify-between gap-2">
        <span className="font-mono text-2xl font-semibold tabular">
          {formatPct(cardPct(gauge))}
        </span>
        <span className="truncate text-[11px] text-fg-muted">
          {gauge.service_name}
          {windowLabel(gauge) ? ` · ${windowLabel(gauge)}` : ''}
        </span>
      </div>
      <Gauge pct={cardPct(gauge)} status={status} size="lg" className="mt-2" />
      <div className="mt-2 flex items-center justify-between">
        <Countdown until={gauge.reset_at} />
        {gauge.error_type ? (
          <span className="text-[11px] text-critical">collector error</span>
        ) : null}
      </div>
    </Card>
  );
}

export function providerPath(providerId: string, accountId: string): string {
  const account = accountId && accountId !== 'default' ? `?account=${encodeURIComponent(accountId)}` : '';
  return `/provider/${encodeURIComponent(providerId)}${account}`;
}
