// "Add a sidecar" — one-click download of the right installer for the
// viewer's OS (macOS .dmg / Windows setup.exe), with every other build one
// click further, the first-run steps, and one-time pairing (PairSidecarPanel). Data: GET /system/sidecar-downloads
// (the server proxies + caches the GitHub release so the browser never calls
// api.github.com directly).

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Download, ExternalLink, ShieldCheck } from 'lucide-react';
import { fetchSidecarDownloads } from '@/api/endpoints';
import type { SidecarChannel, SidecarDownloadAsset } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Button, buttonVariants } from '@/components/ui/Button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { cn } from '@/lib/cn';
import {
  PLATFORM_LABEL,
  assetKindLabel,
  detectViewerOS,
  formatBytes,
  primaryAsset,
  type ViewerOS,
} from './downloads';
import { PairSidecarPanel } from './PairSidecarPanel';

// Step 1 of the first-run list, keyed by what the big button actually
// downloads (older releases only carry the portable builds).
function firstLaunchStep(os: ViewerOS | null, primary: SidecarDownloadAsset | null): string {
  const installer = primary?.kind === 'installer';
  if (os === 'macOS') {
    return `${
      installer
        ? 'Open the DMG and drag Runway Sidecar into Applications.'
        : 'Unzip it and move Runway Sidecar.app into Applications.'
    } First launch: right-click it → Open (the app is not notarized).`;
  }
  if (os === 'Windows') {
    return installer
      ? 'Run the installer (no admin rights needed). If SmartScreen appears: More info → Run anyway.'
      : 'Unzip it and run RunwaySidecar.exe. If SmartScreen appears: More info → Run anyway.';
  }
  return 'Extract the archive and run ./RunwaySidecar (tray) or ./runway-sidecar-cli --daemon.';
}

export function AddSidecarCard({ className }: { className?: string }) {
  const [channel, setChannel] = useState<SidecarChannel>('stable');
  const os = detectViewerOS(typeof navigator === 'undefined' ? undefined : navigator);
  const downloads = useQuery({
    queryKey: ['system', 'sidecar-downloads', channel],
    queryFn: () => fetchSidecarDownloads(channel),
    staleTime: 10 * 60_000,
  });

  const assets = downloads.data?.assets ?? [];
  const primary = primaryAsset(assets, os);
  const others = assets.filter((a) => a !== primary);

  return (
    <Card className={className}>
      <CardHeader>
        <CardTitle>Add a sidecar</CardTitle>
        <div role="group" aria-label="Release channel" className="flex gap-1">
          {(['stable', 'edge'] as const).map((c) => (
            <Button
              key={c}
              size="sm"
              variant={channel === c ? 'primary' : 'ghost'}
              aria-pressed={channel === c}
              onClick={() => setChannel(c)}
            >
              {c === 'stable' ? 'Stable' : 'Edge'}
            </Button>
          ))}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-xs text-fg-muted">
          The sidecar runs on each machine you use AI tools from and forwards local usage to this
          server.
          {channel === 'edge' ? ' Edge is rebuilt on every push to main. It is not a stable release.' : ''}
        </p>

        {downloads.isPending ? (
          <Skeleton className="h-20" />
        ) : downloads.isError || downloads.data?.error || assets.length === 0 ? (
          <div className="rounded-sm border border-edge bg-surface-2 p-3 text-xs text-fg-muted">
            Couldn't load the download list
            {downloads.data?.error ? ` (${downloads.data.error})` : ''}.{' '}
            <a
              className="text-accent hover:underline"
              href={downloads.data?.release_url ?? 'https://github.com/s3ntin3l8/runway-ai-usage-tracker/releases'}
              target="_blank"
              rel="noreferrer"
            >
              Open the releases page
            </a>
          </div>
        ) : (
          <>
            {primary ? (
              <div className="flex flex-wrap items-center gap-3">
                <a
                  href={primary.url}
                  download
                  className={buttonVariants({ variant: 'primary', size: 'lg' })}
                >
                  <Download className="size-4" aria-hidden />
                  Download for {os}
                </a>
                <span className="text-xs text-fg-muted">
                  {assetKindLabel(primary)}
                  {formatBytes(primary.size) ? ` · ${formatBytes(primary.size)}` : ''}
                  {downloads.data?.version ? ` · ${downloads.data.version}` : ''}
                </span>
              </div>
            ) : null}

            <details className="group" open={!primary}>
              <summary className="cursor-pointer text-xs font-medium text-fg-muted hover:text-fg">
                {primary ? 'Other platforms & portable builds' : 'All downloads'}
              </summary>
              <ul className="mt-2 divide-y divide-edge rounded-sm border border-edge">
                {others.map((a) => (
                  <AssetRow key={a.name} asset={a} />
                ))}
              </ul>
            </details>

            <ol className="list-decimal space-y-1 pl-4 text-xs text-fg-muted">
              <li>{firstLaunchStep(os, primary)}</li>
              <li>
                Pair it with this server using the one-time link below. Or open{' '}
                <span className="text-fg">Settings…</span> from the sidecar's tray/menu-bar icon and
                enter this server's URL (
                <code className="font-mono text-fg">{window.location.origin}</code>) and your ingest
                API key yourself.
              </li>
              <li>The sidecar shows up here on its first check-in.</li>
            </ol>

            <p className="flex flex-wrap items-center gap-1.5 text-[11px] text-fg-subtle">
              <ShieldCheck className="size-3.5" aria-hidden />
              {downloads.data?.checksums_url
                ? 'Every build has a SHA-256 checksum and a Sigstore signature.'
                : 'Every build has a SHA-256 checksum.'}
              <a
                className="inline-flex items-center gap-0.5 hover:text-fg"
                href={downloads.data?.release_url}
                target="_blank"
                rel="noreferrer"
              >
                Release notes <ExternalLink className="size-3" aria-hidden />
              </a>
            </p>
          </>
        )}
        <PairSidecarPanel />
      </CardContent>
    </Card>
  );
}

function AssetRow({ asset }: { asset: SidecarDownloadAsset }) {
  const size = formatBytes(asset.size);
  return (
    <li className="flex items-center justify-between gap-2 px-3 py-2 text-xs">
      <div className="min-w-0">
        <div className="flex items-center gap-1.5">
          <span className="font-medium">{PLATFORM_LABEL[asset.platform]}</span>
          <Badge variant={asset.kind === 'installer' ? 'accent' : 'outline'}>
            {asset.kind === 'installer' ? 'Installer' : 'Portable'}
          </Badge>
        </div>
        <div className={cn('truncate text-fg-subtle')}>
          {assetKindLabel(asset)}
          {size ? ` · ${size}` : ''}
          {asset.sha256_url ? (
            <>
              {' · '}
              <a className="hover:text-fg" href={asset.sha256_url}>
                sha256
              </a>
            </>
          ) : null}
        </div>
      </div>
      <a
        href={asset.url}
        download
        aria-label={`Download ${asset.name}`}
        className="inline-flex size-8 shrink-0 items-center justify-center rounded-sm text-fg-muted hover:bg-surface-2 hover:text-fg"
      >
        <Download className="size-4" aria-hidden />
      </a>
    </li>
  );
}
