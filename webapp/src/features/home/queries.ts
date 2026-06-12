// Home-screen data hooks. Polling intervals are tiered by volatility:
// gauges fastest, slow-moving aggregates and diagnostics on long ticks.

import { useQuery } from '@tanstack/react-query';
import {
  fetchAnomalies,
  fetchCostForecast,
  fetchCumulative,
  fetchFleetUsage,
  fetchForecast,
  fetchProviderConfigs,
  fetchTokenHealth,
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

export const useCostForecast = () =>
  useQuery({
    queryKey: ['usage', 'cost-forecast'],
    queryFn: () => fetchCostForecast(),
    refetchInterval: 120_000,
  });

export const useCumulative = () =>
  useQuery({
    queryKey: ['usage', 'cumulative'],
    queryFn: () => fetchCumulative(),
    refetchInterval: 120_000,
  });

export const useTokenHealth = () =>
  useQuery({
    queryKey: ['system', 'token-health'],
    queryFn: fetchTokenHealth,
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
