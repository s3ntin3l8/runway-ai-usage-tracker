import type { SidecarDownloadAsset } from '@/api/types';
import { assetKindLabel, detectViewerOS, formatBytes, primaryAsset } from './downloads';

const asset = (o: Partial<SidecarDownloadAsset>): SidecarDownloadAsset => ({
  platform: 'macOS',
  kind: 'installer',
  name: 'Runway-Sidecar-macOS-v2.13.0.dmg',
  url: 'https://example/x',
  ...o,
});

const ASSETS: SidecarDownloadAsset[] = [
  asset({}),
  asset({ kind: 'payload', name: 'Runway-Sidecar-macOS-v2.13.0.zip' }),
  asset({ platform: 'Windows', name: 'Runway-Sidecar-Windows-v2.13.0-setup.exe' }),
  asset({ platform: 'Windows', kind: 'payload', name: 'Runway-Sidecar-Windows-v2.13.0.zip' }),
  asset({ platform: 'Linux', kind: 'payload', name: 'Runway-Sidecar-Linux-v2.13.0.tar.gz' }),
  asset({ platform: 'Linux-CLI', kind: 'payload', name: 'Runway-Sidecar-Linux-CLI-v2.13.0.tar.gz' }),
];

describe('detectViewerOS', () => {
  it.each([
    [{ userAgentData: { platform: 'macOS' } }, 'macOS'],
    [{ platform: 'MacIntel', userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0)' }, 'macOS'],
    [{ platform: 'Win32', userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)' }, 'Windows'],
    [{ userAgentData: { platform: 'Windows' } }, 'Windows'],
    [{ platform: 'Linux x86_64', userAgent: 'Mozilla/5.0 (X11; Linux x86_64)' }, 'Linux'],
    [{ userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)' }, null],
    [{ userAgent: 'Mozilla/5.0 (Linux; Android 14)' }, null],
    [{}, null],
    [undefined, null],
  ])('%j → %s', (nav, expected) => {
    expect(detectViewerOS(nav)).toBe(expected);
  });
});

describe('primaryAsset', () => {
  it('prefers the installer for macOS and Windows', () => {
    expect(primaryAsset(ASSETS, 'macOS')?.name).toMatch(/\.dmg$/);
    expect(primaryAsset(ASSETS, 'Windows')?.name).toMatch(/-setup\.exe$/);
  });

  it('uses the tray build (not the CLI) on Linux', () => {
    expect(primaryAsset(ASSETS, 'Linux')?.name).toBe('Runway-Sidecar-Linux-v2.13.0.tar.gz');
  });

  it('falls back to the portable build for releases without installers', () => {
    const legacy = ASSETS.filter((a) => a.kind === 'payload');
    expect(primaryAsset(legacy, 'macOS')?.name).toMatch(/\.zip$/);
  });

  it('has no primary for an unknown OS', () => {
    expect(primaryAsset(ASSETS, null)).toBeNull();
  });
});

describe('labels', () => {
  it('describes each asset type', () => {
    expect(assetKindLabel(ASSETS[0])).toMatch(/dmg/);
    expect(assetKindLabel(ASSETS[2])).toMatch(/exe/);
    expect(assetKindLabel(ASSETS[3])).toMatch(/zip/);
    expect(assetKindLabel(ASSETS[4])).toMatch(/tar\.gz/);
  });

  it('formats sizes', () => {
    expect(formatBytes(null)).toBeNull();
    expect(formatBytes(0)).toBeNull();
    expect(formatBytes(512)).toBe('1 KB');
    expect(formatBytes(5.5 * 1024 * 1024)).toBe('5.5 MB');
    expect(formatBytes(42 * 1024 * 1024)).toBe('42 MB');
  });
});
