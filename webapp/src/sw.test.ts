// @vitest-environment jsdom
//
// sw.ts runs its logic as import-time side effects (self.addEventListener
// wiring inside workbox internals), so each test mocks the workbox modules,
// stubs the ServiceWorkerGlobalScope bits sw.ts calls directly on `self`, and
// re-imports the module fresh. What's asserted is the actual bug this file
// fixes: the NetworkOnly navigation-route matcher (the core of the fix) and
// the offline-only catch-handler fallback — not just "the module loads".
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const precacheAndRoute = vi.fn();
const cleanupOutdatedCaches = vi.fn();
const matchPrecache = vi.fn();
const registerRoute = vi.fn();
const setCatchHandler = vi.fn();
const clientsClaim = vi.fn();

vi.mock('workbox-precaching', () => ({ precacheAndRoute, cleanupOutdatedCaches, matchPrecache }));
vi.mock('workbox-routing', () => ({ registerRoute, setCatchHandler }));
vi.mock('workbox-strategies', () => ({ NetworkOnly: class NetworkOnly {} }));
vi.mock('workbox-core', () => ({ clientsClaim }));

const skipWaiting = vi.fn();

beforeEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
  (self as unknown as { skipWaiting: typeof skipWaiting }).skipWaiting = skipWaiting;
  (self as unknown as { __WB_MANIFEST: unknown }).__WB_MANIFEST = [];
});

afterEach(() => {
  delete (self as unknown as { skipWaiting?: unknown }).skipWaiting;
  delete (self as unknown as { __WB_MANIFEST?: unknown }).__WB_MANIFEST;
});

describe('sw.ts', () => {
  it('precaches the manifest, cleans up outdated caches, and takes over immediately', async () => {
    await import('./sw');
    expect(precacheAndRoute).toHaveBeenCalledWith([]);
    expect(cleanupOutdatedCaches).toHaveBeenCalledTimes(1);
    expect(skipWaiting).toHaveBeenCalledTimes(1);
    expect(clientsClaim).toHaveBeenCalledTimes(1);
  });

  describe('the navigation route (the actual bug fix)', () => {
    async function getMatcher() {
      await import('./sw');
      expect(registerRoute).toHaveBeenCalledTimes(1);
      return registerRoute.mock.calls[0][0] as (ctx: {
        request: { mode: string };
        url: URL;
      }) => boolean;
    }

    it('matches a top-level navigation outside /api — this is what must hit the network', async () => {
      const matches = await getMatcher();
      expect(matches({ request: { mode: 'navigate' }, url: new URL('https://runway.example/') })).toBe(
        true,
      );
      expect(
        matches({ request: { mode: 'navigate' }, url: new URL('https://runway.example/history') }),
      ).toBe(true);
    });

    it('excludes /api navigations, so ingest/etc. are never routed through this handler', async () => {
      const matches = await getMatcher();
      expect(
        matches({
          request: { mode: 'navigate' },
          url: new URL('https://runway.example/api/v1/system/settings'),
        }),
      ).toBe(false);
    });

    it('ignores non-navigation requests (images, scripts, XHR) entirely', async () => {
      const matches = await getMatcher();
      expect(matches({ request: { mode: 'cors' }, url: new URL('https://runway.example/') })).toBe(
        false,
      );
      expect(matches({ request: { mode: 'no-cors' }, url: new URL('https://runway.example/logo.svg') })).toBe(
        false,
      );
    });

    it('registers the route with a NetworkOnly strategy (never cache-first)', async () => {
      await import('./sw');
      const strategy = registerRoute.mock.calls[0][1];
      expect(strategy.constructor.name).toBe('NetworkOnly');
    });
  });

  describe('the offline catch handler', () => {
    async function getCatchHandler() {
      await import('./sw');
      expect(setCatchHandler).toHaveBeenCalledTimes(1);
      return setCatchHandler.mock.calls[0][0] as (ctx: {
        request: { mode: string };
      }) => Promise<Response>;
    }

    it('serves the precached shell for a failed navigation (genuine offline)', async () => {
      const shellResponse = new Response('<html>offline shell</html>');
      matchPrecache.mockResolvedValue(shellResponse);
      const handleCatch = await getCatchHandler();

      const result = await handleCatch({ request: { mode: 'navigate' } });

      expect(matchPrecache).toHaveBeenCalledWith('/index.html');
      expect(result).toBe(shellResponse);
    });

    it('does not serve the cached shell for a failed non-navigation request', async () => {
      const handleCatch = await getCatchHandler();

      const result = await handleCatch({ request: { mode: 'cors' } });

      expect(matchPrecache).not.toHaveBeenCalled();
      expect(result).toBeInstanceOf(Response);
      expect(result.type).toBe('error');
    });

    it('falls through to a network-error Response if the shell was never precached', async () => {
      matchPrecache.mockResolvedValue(undefined);
      const handleCatch = await getCatchHandler();

      const result = await handleCatch({ request: { mode: 'navigate' } });

      expect(result).toBeInstanceOf(Response);
      expect(result.type).toBe('error');
    });
  });
});
