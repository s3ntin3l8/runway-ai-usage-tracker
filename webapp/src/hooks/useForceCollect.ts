// Shared "force a fresh collection" mutation — triggers a server-side collect
// (real provider API calls + sidecar fan-out) and refreshes the data queries.
// Used by the Home "Collect now" button and the app-wide pull-to-refresh gesture
// so both share one code path and one toast/invalidate behavior.

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { forceCollect } from '@/api/endpoints';

export function useForceCollect() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: forceCollect,
    onSuccess: (result) => {
      toast.success(
        `Collection triggered — ${result.cards} cards, ${result.sidecars_triggered} sidecars`,
      );
      queryClient.invalidateQueries({ queryKey: ['usage'] });
      queryClient.invalidateQueries({ queryKey: ['fleet'] });
      queryClient.invalidateQueries({ queryKey: ['system', 'token-health'] });
    },
    onError: (err) => toast.error(`Collection failed: ${err.message}`),
  });
}
