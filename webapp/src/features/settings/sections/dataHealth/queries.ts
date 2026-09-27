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

export const useDataHealthReport = () =>
  useQuery({
    queryKey: dataHealthKey,
    queryFn: fetchDataHealthReport,
    refetchInterval: (query) => (query.state.data?.scanning ? 2_000 : 60_000),
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
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 1_000 : false),
  });
