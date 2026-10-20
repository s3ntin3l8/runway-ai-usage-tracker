// About: server identity, version, collector status snapshot.

import type { ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Download, RefreshCw } from 'lucide-react';
import { toast } from 'sonner';
import { checkForUpdates, fetchSettings, fetchStatus } from '@/api/endpoints';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { useInstallPrompt } from '@/hooks/useInstallPrompt';

const RELEASES_URL = 'https://github.com/s3ntin3l8/runway-ai-usage-tracker/releases';
const COMMIT_URL = 'https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit';

// How much of the SHA to show. The full 40 chars overflow the grid cell; the
// link still carries the whole SHA, and the hover title spells it out.
const SHORT_SHA_LEN = 12;

// Friendly labels for the `auth_methods` the /settings probe advertises —
// raw values (`admin_key`, `forward_auth`) are internal wire strings.
const AUTH_METHOD_LABELS: Record<string, string> = {
  admin_key: 'Admin key',
  forward_auth: 'Forward Auth (SSO)',
};

function formatAuthMethods(methods: string[] | undefined): string {
  if (!methods || methods.length === 0) return '—';
  return methods.map((m) => AUTH_METHOD_LABELS[m] ?? m).join(', ');
}

export function AboutSection() {
  const queryClient = useQueryClient();
  const { canInstall, promptInstall } = useInstallPrompt();
  const settings = useQuery({ queryKey: ['system', 'settings'], queryFn: fetchSettings });
  const status = useQuery({
    queryKey: ['system', 'status'],
    queryFn: fetchStatus,
    refetchInterval: 60_000,
  });

  const check = useMutation({
    mutationFn: checkForUpdates,
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ['system', 'settings'] });
      queryClient.invalidateQueries({ queryKey: ['fleet', 'sidecars'] });
      toast.success(
        res.update_available
          ? `Runway v${res.latest_version} is available`
          : `You're on the latest release (v${res.current_version})`,
      );
    },
    onError: (err) => toast.error(err.message),
  });

  if (settings.isPending) return <Skeleton className="h-48 max-w-2xl" />;

  const s = settings.data;

  return (
    <div className="flex max-w-2xl flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>{s?.project_name ?? 'Runway'}</CardTitle>
          <div className="flex items-center gap-2">
            <Badge variant="accent">v{s?.version ?? '?'}</Badge>
            {s?.update_available ? (
              <a href={RELEASES_URL} target="_blank" rel="noreferrer">
                <Badge variant="warning">v{s.latest_version} available</Badge>
              </a>
            ) : null}
          </div>
        </CardHeader>
        <CardContent>
          {/* Action buttons on their own row so they never crowd the title line */}
          <div className="mb-4 flex flex-wrap gap-2">
            <Button
              size="sm"
              variant="ghost"
              onClick={() => check.mutate()}
              loading={check.isPending}
            >
              <RefreshCw className="size-3.5" aria-hidden />
              Check for updates
            </Button>
            {canInstall ? (
              <Button size="sm" variant="ghost" onClick={() => void promptInstall()}>
                <Download className="size-3.5" aria-hidden />
                Install app
              </Button>
            ) : null}
          </div>
          <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-[13px] sm:grid-cols-3">
            <InfoItem label="Host" value={`${s?.app_host ?? '—'}:${s?.app_port ?? ''}`} />
            <InfoItem label="Encryption" value={s?.encryption_enabled ? 'enabled' : 'off'} />
            <InfoItem label="Admin auth" value={s?.admin_auth_required ? 'required' : 'open'} />
            <InfoItem label="Auth methods" value={formatAuthMethods(s?.auth_methods)} />
            <InfoItem label="User" value={s?.user_context ?? 'local'} />
            <InfoItem label="Build" value={<BuildCommit sha={s?.build_sha} />} />
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

function InfoItem({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div>
      <dt className="text-[11px] text-fg-subtle">{label}</dt>
      <dd className="mt-0.5 truncate font-medium">{value}</dd>
    </div>
  );
}

// The commit the running server was built from — what identifies one exact
// build (every `:edge` image ships the same version string). Links to the
// commit so a reader can see the diff; plain "—" when the build carries no SHA.
function BuildCommit({ sha }: { sha?: string | null }) {
  if (!sha) return <>—</>;
  return (
    <a
      href={`${COMMIT_URL}/${sha}`}
      target="_blank"
      rel="noreferrer"
      title={`Commit ${sha}`}
      className="font-mono tabular text-accent hover:underline"
    >
      {sha.slice(0, SHORT_SHA_LEN)}
    </a>
  );
}
