// Data Health query hooks. The report poll interval self-adjusts: fast while
// a scan is in flight (`scanning: true`), slow once it settles — so opening
// the page right after a fix doesn't need a manual refresh, but a quiet page
// doesn't hammer the endpoint either. Shared query key with the Home page's
// error badge so both read one cached report instead of triggering their own
// scans.

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  applyDataHealthFix,
  fetchDataHealthJob,
  fetchDataHealthReport,
  previewDataHealthFix,
  rescanDataHealth,
} from '@/api/endpoints';

export const dataHealthKey = ['system', 'data-health'] as const;

// The report GET is rate-limited at 30/minute and job status at 120/minute
// (see app/api/endpoints/data_health.py). Five-second polling stays below
// both limits, including when two tabs are open.
const SCANNING_INTERVAL_MS = 5_000;

export const useDataHealthReport = (quietIntervalMs = 60_000) =>
  useQuery({
    queryKey: dataHealthKey,
    queryFn: fetchDataHealthReport,
    refetchInterval: (query) =>
      query.state.data?.scanning ? SCANNING_INTERVAL_MS : quietIntervalMs,
    // Admin-gated: a locked-down remote instance may 403 — the badge/section
    // just hides rather than retrying into a visible error.
    retry: false,
  });

export const useRescanDataHealth = () => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: rescanDataHealth,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: dataHealthKey }),
  });
};

export const usePreviewDataHealthFix = () =>
  useMutation({
    mutationFn: ({
      checkId,
      groupKey,
      params,
    }: {
      checkId: string;
      groupKey: string;
      params: Record<string, unknown>;
    }) => previewDataHealthFix(checkId, groupKey, params),
  });

export const useApplyDataHealthFix = () =>
  useMutation({
    mutationFn: ({
      checkId,
      groupKey,
      params,
    }: {
      checkId: string;
      groupKey: string;
      params: Record<string, unknown>;
    }) => applyDataHealthFix(checkId, groupKey, params),
  });

export const useDataHealthJob = (jobId: string | null) =>
  useQuery({
    queryKey: ['system', 'data-health', 'job', jobId],
    queryFn: () => fetchDataHealthJob(jobId as string),
    enabled: jobId !== null,
    retry: false,
    // Keep checking through transient errors. Unknown status is not a terminal job state.
    refetchInterval: (query) =>
      query.state.data?.status === 'succeeded' || query.state.data?.status === 'failed'
        ? false
        : 5_000,
  });
