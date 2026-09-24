import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import * as api from '@/api/endpoints';
import type { SidecarDownloads } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { AddSidecarCard } from './AddSidecarCard';

vi.mock('@/api/endpoints');

const release = (channel: 'stable' | 'edge' = 'stable'): SidecarDownloads => {
  const label = channel === 'edge' ? 'edge' : 'v2.13.0';
  const mk = (platform: SidecarDownloads['assets'][number]['platform'], kind: 'installer' | 'payload', name: string) => ({
    platform,
    kind,
    name,
    url: `https://example/${name}`,
    size: 30 * 1024 * 1024,
    sha256_url: `https://example/${name}.sha256`,
  });
  return {
    channel,
    version: label,
    release_url: `https://example/releases/tag/${label}`,
    assets: [
      mk('macOS', 'installer', `Runway-Sidecar-macOS-${label}.dmg`),
      mk('macOS', 'payload', `Runway-Sidecar-macOS-${label}.zip`),
      mk('Windows', 'installer', `Runway-Sidecar-Windows-${label}-setup.exe`),
      mk('Windows', 'payload', `Runway-Sidecar-Windows-${label}.zip`),
      mk('Linux', 'payload', `Runway-Sidecar-Linux-${label}.tar.gz`),
      mk('Linux-CLI', 'payload', `Runway-Sidecar-Linux-CLI-${label}.tar.gz`),
    ],
  };
};

function stubPlatform(platform: string, userAgent: string) {
  vi.spyOn(navigator, 'platform', 'get').mockReturnValue(platform);
  vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(userAgent);
}

describe('AddSidecarCard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.restoreAllMocks();
    vi.mocked(api.fetchSidecarDownloads).mockImplementation(async (c) => release(c));
    vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [] });
  });

  it('offers the Windows installer to a Windows viewer', async () => {
    stubPlatform('Win32', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)');
    renderWithProviders(<AddSidecarCard />);
    const link = await screen.findByRole('link', { name: /download for windows/i });
    expect(link).toHaveAttribute('href', 'https://example/Runway-Sidecar-Windows-v2.13.0-setup.exe');
    expect(screen.getByText(/SmartScreen/)).toBeInTheDocument();
  });

  it('offers the DMG to a macOS viewer', async () => {
    stubPlatform('MacIntel', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0)');
    renderWithProviders(<AddSidecarCard />);
    const link = await screen.findByRole('link', { name: /download for macos/i });
    expect(link.getAttribute('href')).toMatch(/\.dmg$/);
  });

  it('lists every other build, including checksums', async () => {
    stubPlatform('MacIntel', 'Mozilla/5.0 (Macintosh)');
    renderWithProviders(<AddSidecarCard />);
    await screen.findByRole('link', { name: /download for macos/i });
    for (const name of [
      'Runway-Sidecar-Windows-v2.13.0-setup.exe',
      'Runway-Sidecar-Linux-CLI-v2.13.0.tar.gz',
      'Runway-Sidecar-macOS-v2.13.0.zip',
    ]) {
      expect(screen.getByRole('link', { name: `Download ${name}` })).toBeInTheDocument();
    }
    expect(screen.getAllByRole('link', { name: 'sha256' }).length).toBe(5);
  });

  it('adapts the first-run step and signature claim to legacy zip-only releases', async () => {
    stubPlatform('MacIntel', 'Mozilla/5.0 (Macintosh)');
    const legacy = release();
    legacy.assets = legacy.assets.filter((a) => a.kind === 'payload');
    vi.mocked(api.fetchSidecarDownloads).mockResolvedValue({ ...legacy, checksums_url: null });
    renderWithProviders(<AddSidecarCard />);
    const link = await screen.findByRole('link', { name: /download for macos/i });
    expect(link.getAttribute('href')).toMatch(/\.zip$/);
    expect(screen.getByText(/Unzip it and move Runway Sidecar.app/)).toBeInTheDocument();
    expect(screen.queryByText(/Sigstore/)).not.toBeInTheDocument();
  });

  it('mentions Sigstore only when the release carries SHA256SUMS', async () => {
    stubPlatform('MacIntel', 'Mozilla/5.0 (Macintosh)');
    vi.mocked(api.fetchSidecarDownloads).mockResolvedValue({
      ...release(),
      checksums_url: 'https://example/SHA256SUMS.txt',
    });
    renderWithProviders(<AddSidecarCard />);
    expect(await screen.findByText(/Sigstore signature/)).toBeInTheDocument();
    expect(screen.getByText(/Open the DMG/)).toBeInTheDocument();
  });

  it('includes the pairing step', async () => {
    renderWithProviders(<AddSidecarCard />);
    expect(
      await screen.findByRole('button', { name: /generate pairing link/i }),
    ).toBeInTheDocument();
  });

  it('switches to the edge channel', async () => {
    stubPlatform('Win32', 'Mozilla/5.0 (Windows NT 10.0)');
    renderWithProviders(<AddSidecarCard />);
    await screen.findByRole('link', { name: /download for windows/i });
    await userEvent.click(screen.getByRole('button', { name: 'Edge' }));
    const link = await screen.findByRole('link', { name: /download for windows/i });
    expect(link).toHaveAttribute('href', 'https://example/Runway-Sidecar-Windows-edge-setup.exe');
    expect(api.fetchSidecarDownloads).toHaveBeenCalledWith('edge');
  });

  it('falls back to the releases page when GitHub is unreachable', async () => {
    vi.mocked(api.fetchSidecarDownloads).mockResolvedValue({
      channel: 'stable',
      release_url: 'https://example/releases',
      assets: [],
      error: 'GitHub unreachable: ConnectError',
    });
    renderWithProviders(<AddSidecarCard />);
    const link = await screen.findByRole('link', { name: /open the releases page/i });
    expect(link).toHaveAttribute('href', 'https://example/releases');
    expect(screen.getByText(/ConnectError/)).toBeInTheDocument();
  });
});
