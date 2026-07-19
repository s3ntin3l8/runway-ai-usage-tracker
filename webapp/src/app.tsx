import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { lazy, Suspense } from 'react';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router';
import { Toaster, toast } from 'sonner';
import { ApiError } from '@/api/client';
import { AppShell } from '@/components/layout/AppShell';
import { BootGate } from '@/features/auth/BootGate';
import { ThemeProvider } from '@/hooks/useTheme';
import { InstallProvider } from '@/hooks/useInstallPrompt';
import { ExcludeCacheProvider } from '@/hooks/useExcludeCache';
import { TooltipProvider } from '@/components/ui/Tooltip';
import { HomePage } from '@/features/home/HomePage';
import { createAuthRedirectGuard, createQueryErrorAuthRedirectHandler } from '@/lib/authRedirect';

const ProviderPage = lazy(() =>
  import('@/features/provider/ProviderPage').then((m) => ({ default: m.ProviderPage })),
);
const InsightsPage = lazy(() =>
  import('@/features/insights/InsightsPage').then((m) => ({ default: m.InsightsPage })),
);
const HistoryPage = lazy(() =>
  import('@/features/history/HistoryPage').then((m) => ({ default: m.HistoryPage })),
);
const FleetPage = lazy(() =>
  import('@/features/fleet/FleetPage').then((m) => ({ default: m.FleetPage })),
);
const SettingsPage = lazy(() =>
  import('@/features/settings/SettingsPage').then((m) => ({ default: m.SettingsPage })),
);
const KitPage = lazy(() => import('@/features/dev/KitPage').then((m) => ({ default: m.KitPage })));

// With the service worker's network-first navigation route (see sw.ts), this
// reload is a real top-level request and the browser follows the upstream
// SSO 302 straight to the login page — no interstitial needed for the common
// case of the session expiring while the dashboard is already open.
const handleAuthRedirect = createAuthRedirectGuard(() => {
  toast.error('Your session has expired. Signing in again…');
  window.setTimeout(() => window.location.reload(), 1500);
});

const queryClient = new QueryClient({
  // BootGate's own settings query is excluded (via meta) since it owns its
  // own "Session expired" UI — see lib/authRedirect.ts.
  queryCache: new QueryCache({ onError: createQueryErrorAuthRedirectHandler(handleAuthRedirect) }),
  mutationCache: new MutationCache({ onError: handleAuthRedirect }),
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      // Server errors are worth one retry; 4xx (auth, validation) are not.
      // An authRedirect (upstream SSO bounce) also isn't worth retrying — the
      // session is gone until the user signs back in, so retrying just delays
      // BootGate showing the "Session expired" screen.
      retry: (failureCount, error) =>
        error instanceof ApiError &&
        !error.authRedirect &&
        (error.status === 0 || error.status >= 500)
          ? failureCount < 2
          : false,
    },
  },
});

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <InstallProvider>
          <ExcludeCacheProvider>
            <BootGate>
              <TooltipProvider>
                <BrowserRouter>
                  <Suspense fallback={null}>
                    <Routes>
                      <Route element={<AppShell />}>
                        <Route index element={<HomePage />} />
                        <Route path="provider/:providerId" element={<ProviderPage />} />
                        <Route path="insights" element={<InsightsPage />} />
                        <Route path="history" element={<HistoryPage />} />
                        <Route path="fleet" element={<FleetPage />} />
                        <Route path="settings/*" element={<SettingsPage />} />
                        {import.meta.env.DEV ? <Route path="dev/kit" element={<KitPage />} /> : null}
                        <Route path="*" element={<Navigate to="/" replace />} />
                      </Route>
                    </Routes>
                  </Suspense>
                </BrowserRouter>
              </TooltipProvider>
            </BootGate>
            <Toaster position="bottom-right" theme="system" />
          </ExcludeCacheProvider>
        </InstallProvider>
      </ThemeProvider>
    </QueryClientProvider>
  );
}
