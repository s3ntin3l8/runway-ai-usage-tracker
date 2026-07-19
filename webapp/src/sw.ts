/// <reference lib="webworker" />
// Custom service worker (injectManifest strategy — see vite.config.ts for why
// generateSW's built-in navigateFallback can't do this: its NavigationRoute is
// cache-first and registers ahead of any runtimeCaching route, so it always
// wins).
//
// Runway is often deployed behind a forward-auth reverse proxy (Authentik,
// oauth2-proxy, Authelia). When the upstream SSO session expires, a top-level
// navigation to '/' gets a 302 to the proxy's login page. Browsers make
// navigation requests with redirect:'manual', so fetch()ing one that hits a
// redirect resolves to an opaque `type: 'opaqueredirect'` response rather than
// throwing — returning that response from respondWith() makes the browser
// follow the redirect itself. A cache-first navigateFallback would instead
// serve the precached shell and hide the redirect entirely, which is the bug
// this file exists to avoid (see BootGate.tsx's authRedirect handling for the
// in-app-fetch half of the same problem).
//
// So: navigations always go to the network (NetworkOnly, never cached), and
// the precached index.html is used only as a genuine offline fallback.
import { cleanupOutdatedCaches, matchPrecache, precacheAndRoute } from 'workbox-precaching';
import { registerRoute, setCatchHandler } from 'workbox-routing';
import { NetworkOnly } from 'workbox-strategies';
import { clientsClaim } from 'workbox-core';

declare let self: ServiceWorkerGlobalScope;

precacheAndRoute(self.__WB_MANIFEST);
cleanupOutdatedCaches();

registerRoute(
  ({ request, url }) => request.mode === 'navigate' && !url.pathname.startsWith('/api/'),
  new NetworkOnly(),
);

// Gated on navigate: setCatchHandler is global, and serving cached HTML for a
// failed image/font/script request would be worse than just letting it fail.
setCatchHandler(async ({ request }) => {
  if (request.mode === 'navigate') {
    const shell = await matchPrecache('/index.html');
    if (shell) return shell;
  }
  return Response.error();
});

// generateSW wires skipWaiting/clientsClaim in for registerType:'autoUpdate'
// automatically; injectManifest does not, so this is load-bearing — without
// it, new deploys silently stop taking over open tabs.
self.skipWaiting();
clientsClaim();
