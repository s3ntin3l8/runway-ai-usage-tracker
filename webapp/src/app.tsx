import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { lazy, Suspense } from 'react';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router';
import { Toaster } from 'sonner';
import { ApiError } from '@/api/client';
import { AppShell } from '@/components/layout/AppShell';
import { BootGate } from '@/features/auth/BootGate';
import { ThemeProvider } from '@/hooks/useTheme';
import { ExcludeCacheProvider } from '@/hooks/useExcludeCache';
import { HomePage } from '@/features/home/HomePage';

const ProviderPage = lazy(() =>
  import('@/features/provider/ProviderPage').then((m) => ({ default: m.ProviderPage })),
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

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      // Server errors are worth one retry; 4xx (auth, validation) are not.
      retry: (failureCount, error) =>
        error instanceof ApiError && (error.status === 0 || error.status >= 500)
          ? failureCount < 2
          : false,
    },
  },
});

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <ExcludeCacheProvider>
          <BootGate>
            <BrowserRouter>
              <Suspense fallback={null}>
                <Routes>
                  <Route element={<AppShell />}>
                    <Route index element={<HomePage />} />
                    <Route path="provider/:providerId" element={<ProviderPage />} />
                    <Route path="history" element={<HistoryPage />} />
                    <Route path="fleet" element={<FleetPage />} />
                    <Route path="settings/*" element={<SettingsPage />} />
                    {import.meta.env.DEV ? <Route path="dev/kit" element={<KitPage />} /> : null}
                    <Route path="*" element={<Navigate to="/" replace />} />
                  </Route>
                </Routes>
              </Suspense>
            </BrowserRouter>
          </BootGate>
          <Toaster position="bottom-right" theme="system" />
        </ExcludeCacheProvider>
      </ThemeProvider>
    </QueryClientProvider>
  );
}
