// The on/off switch for the Claude cloud-runner collector, with the collector's health lines
// beneath it. The switch saves on change; a failed save rolls it back and says why.
import { useId } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { fetchAppConfig, fetchStatus, putAppConfig } from '@/api/endpoints';
import type { AppConfig } from '@/api/types';
import { Switch } from '@/components/ui/Switch';
import { ClaudeCloudHealthLines } from './ClaudeCloudHealthLines';

const APP_CONFIG_KEY = ['system', 'app-config'];

export function ClaudeCloudCollectorSwitch() {
  const queryClient = useQueryClient();
  const labelId = useId();
  const appConfig = useQuery({ queryKey: APP_CONFIG_KEY, queryFn: fetchAppConfig });
  const status = useQuery({
    queryKey: ['system', 'status'],
    queryFn: fetchStatus,
    refetchInterval: 60_000,
  });

  // Effective state: the app-config value, else what the status endpoint reports.
  const enabled =
    appConfig.data?.claude_cloud_collector_effective ?? status.data?.claude_cloud?.enabled ?? true;

  const save = useMutation({
    mutationFn: (next: boolean) => putAppConfig({ claude_cloud_collector_enabled: next }),
    onMutate: async (next) => {
      await queryClient.cancelQueries({ queryKey: APP_CONFIG_KEY });
      const previous = queryClient.getQueryData<AppConfig>(APP_CONFIG_KEY);
      queryClient.setQueryData<AppConfig>(APP_CONFIG_KEY, {
        ...previous,
        claude_cloud_collector_enabled: next,
        claude_cloud_collector_effective: next,
      });
      return { previous };
    },
    onError: (err, _next, ctx) => {
      queryClient.setQueryData(APP_CONFIG_KEY, ctx?.previous);
      toast.error(err.message);
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: APP_CONFIG_KEY });
      queryClient.invalidateQueries({ queryKey: ['system', 'status'] });
    },
  });

  return (
    <div className="flex flex-col gap-2" data-testid="claude-cloud-collector">
      <div className="flex items-start justify-between gap-3 rounded-md border border-edge bg-surface-1 p-3">
        <div className="min-w-0">
          <p id={labelId} className="text-[13px] font-medium text-fg">
            Cloud-runner collector{' '}
            <span className="text-[11px] font-normal text-fg-muted" data-testid="claude-cloud-state">
              {enabled ? 'On' : 'Off'}
            </span>
          </p>
          <p className="mt-0.5 text-[12px] text-fg-muted">
            Collect usage for Claude Code sessions that run on Anthropic's cloud runner.
          </p>
          {save.isError ? (
            <p role="alert" className="mt-1 text-[12px] text-critical">
              Could not save: {save.error.message}
            </p>
          ) : null}
        </div>
        <Switch
          aria-labelledby={labelId}
          checked={enabled}
          disabled={appConfig.isLoading}
          onCheckedChange={(next) => save.mutate(next)}
        />
      </div>
      <ClaudeCloudHealthLines />
    </div>
  );
}
