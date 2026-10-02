// Home-screen data hooks. Polling intervals are tiered by volatility:
// gauges fastest, slow-moving aggregates and diagnostics on long ticks.

import { useQuery } from '@tanstack/react-query';
import { useExcludeCache } from '@/hooks/useExcludeCache';
import { credentialInventoryKey } from '@/hooks/useInvalidateCredentialViews';
import {
  fetchAnomalies,
  fetchCostForecast,
  fetchCumulative,
  fetchFleetUsage,
  fetchForecast,
  fetchProviderConfigs,
  fetchCredentialInventory,
  getDashboardLayout,
} from '@/api/endpoints';

export const useFleet = () =>
  useQuery({
    queryKey: ['usage', 'fleet'],
    queryFn: fetchFleetUsage,
    refetchInterval: 30_000,
  });

export const useForecast = () =>
  useQuery({
    queryKey: ['usage', 'forecast'],
    queryFn: () => fetchForecast(),
    refetchInterval: 60_000,
  });

export const useCostForecast = () => {
  const { excludeCache } = useExcludeCache();
  return useQuery({
    queryKey: ['usage', 'cost-forecast', excludeCache],
    queryFn: () => fetchCostForecast({ exclude_cache: excludeCache }),
    refetchInterval: 120_000,
  });
};

export const useCumulative = () =>
  useQuery({
    // 'month' scope only — the strip's Tokens card needs just the current
    // local-tz month total, so this skips the default call's full rollup
    // read + year-to-date event scan. Distinct key from provider-page
    // fetchCumulative() calls, which fetch the full (lifetime/year/month) shape.
    queryKey: ['usage', 'cumulative', 'month'],
    queryFn: () => fetchCumulative({ period_type: 'month' }),
    refetchInterval: 120_000,
  });

// The credential inventory, on the Home page's quiet cadence. Same key as the Settings views,
// so a credential mutation refreshes the banner too; the inventory does a request-time scan of
// the server host, so Home polls it every 5 minutes, not every minute.
export const useCredentialBanners = () =>
  useQuery({
    queryKey: credentialInventoryKey,
    queryFn: fetchCredentialInventory,
    refetchInterval: 300_000,
    // Admin-gated: a locked-down remote instance may 403 — banner just hides.
    retry: false,
  });

export const useAnomalies = () =>
  useQuery({
    queryKey: ['usage', 'anomalies'],
    queryFn: () => fetchAnomalies(),
    refetchInterval: 300_000,
  });

export const useProviderConfigs = () =>
  useQuery({
    queryKey: ['system', 'provider-configs'],
    queryFn: fetchProviderConfigs,
    staleTime: 300_000,
  });

export const useDashboardLayout = () =>
  useQuery({
    queryKey: ['system', 'dashboard-layout'],
    queryFn: getDashboardLayout,
    staleTime: Infinity,
  });
