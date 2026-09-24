// Pure helpers behind the "Add a sidecar" card: which OS is the viewer on, and
// which release asset should be the big download button for it.

import type { SidecarDownloadAsset, SidecarPlatform } from '@/api/types';

export type ViewerOS = 'macOS' | 'Windows' | 'Linux';

interface NavigatorLike {
  userAgent?: string;
  platform?: string;
  userAgentData?: { platform?: string };
}

// Client hints first (Chromium), then the legacy platform/UA strings. Mobile
// OSes map to null — there's no sidecar for them, so no primary download.
export function detectViewerOS(nav: NavigatorLike | undefined): ViewerOS | null {
  if (!nav) return null;
  const hint = (nav.userAgentData?.platform ?? '').toLowerCase();
  const plat = (nav.platform ?? '').toLowerCase();
  const ua = (nav.userAgent ?? '').toLowerCase();
  if (/iphone|ipad|ipod|android/.test(ua)) return null;
  const all = `${hint} ${plat} ${ua}`;
  if (/mac/.test(all)) return 'macOS';
  if (/win/.test(all)) return 'Windows';
  if (/linux|x11|cros/.test(all)) return 'Linux';
  return null;
}

// The recommended asset for an OS: its installer where one exists (DMG /
// setup.exe), else the desktop tray payload. Falls back to the portable
// payload for releases that predate the installers.
export function primaryAsset(
  assets: SidecarDownloadAsset[],
  os: ViewerOS | null,
): SidecarDownloadAsset | null {
  if (!os) return null;
  return (
    assets.find((a) => a.platform === os && a.kind === 'installer') ??
    assets.find((a) => a.platform === os && a.kind === 'payload') ??
    null
  );
}

export const PLATFORM_LABEL: Record<SidecarPlatform, string> = {
  macOS: 'macOS (Apple Silicon)',
  Windows: 'Windows',
  Linux: 'Linux desktop (tray)',
  'Linux-CLI': 'Linux headless (CLI)',
};

export function assetKindLabel(a: SidecarDownloadAsset): string {
  if (a.name.endsWith('.dmg')) return 'Disk image (.dmg)';
  if (a.name.endsWith('-setup.exe')) return 'Installer (.exe)';
  if (a.name.endsWith('.tar.gz')) return 'Archive (.tar.gz)';
  return 'Portable (.zip)';
}

export function formatBytes(n: number | null | undefined): string | null {
  if (!n || n <= 0) return null;
  const mb = n / (1024 * 1024);
  return mb >= 1 ? `${mb.toFixed(mb >= 10 ? 0 : 1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;
}
