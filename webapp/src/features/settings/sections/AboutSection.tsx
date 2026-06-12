// About: server identity, version, collector status snapshot.

import { useQuery } from '@tanstack/react-query';
import { fetchSettings, fetchStatus } from '@/api/endpoints';
import { Badge } from '@/components/ui/Badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';

export function AboutSection() {
  const settings = useQuery({ queryKey: ['system', 'settings'], queryFn: fetchSettings });
  const status = useQuery({
    queryKey: ['system', 'status'],
    queryFn: fetchStatus,
    refetchInterval: 60_000,
  });

  if (settings.isPending) return <Skeleton className="h-48 max-w-2xl" />;

  const s = settings.data;

  return (
    <div className="flex max-w-2xl flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>{s?.project_name ?? 'Runway'}</CardTitle>
          <Badge variant="accent">v{s?.version ?? '?'}</Badge>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-[13px] sm:grid-cols-3">
            <InfoItem label="Host" value={`${s?.app_host ?? '—'}:${s?.app_port ?? ''}`} />
            <InfoItem label="Encryption" value={s?.encryption_enabled ? 'enabled' : 'off'} />
            <InfoItem label="Admin auth" value={s?.admin_auth_required ? 'required' : 'open'} />
            <InfoItem label="Auth methods" value={(s?.auth_methods ?? []).join(', ') || '—'} />
            <InfoItem label="User" value={s?.user_context ?? 'local'} />
          </dl>
          {s?.ingest_api_key_is_default ? (
            <p className="mt-3 rounded-sm bg-warning-muted px-3 py-2 text-xs text-warning">
              The ingest API key is still the insecure default — set INGEST_API_KEY before
              connecting remote sidecars.
            </p>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Collector status</CardTitle>
        </CardHeader>
        <CardContent>
          {status.isPending ? (
            <Skeleton className="h-32 w-full" />
          ) : (
            <pre className="max-h-80 overflow-auto rounded-sm bg-surface-2 p-3 font-mono text-[11px] leading-relaxed whitespace-pre-wrap">
              {JSON.stringify(status.data, null, 2)}
            </pre>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function InfoItem({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-[11px] text-fg-subtle">{label}</dt>
      <dd className="mt-0.5 truncate font-medium">{value}</dd>
    </div>
  );
}
