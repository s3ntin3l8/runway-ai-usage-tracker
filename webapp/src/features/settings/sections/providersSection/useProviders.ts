import { useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { putDashboardLayout } from '@/api/endpoints';
import type { DashboardLayout } from '@/api/types';
import { useDashboardLayout, useProviderConfigs } from '@/features/home/queries';

export interface UseProvidersResult {
  configs: ReturnType<typeof useProviderConfigs>;
  layout: ReturnType<typeof useDashboardLayout>;
  saveOrder: ReturnType<typeof useMutation<{ status: string }, Error, string[]>>;
}

export function useProviders(): UseProvidersResult {
  const configs = useProviderConfigs();
  const layout = useDashboardLayout();
  const queryClient = useQueryClient();
  const saveOrder = useMutation({
    mutationFn: (orderedProviderIds: string[]) =>
      putDashboardLayout({
        provider_order: orderedProviderIds,
        card_orders: layout.data?.card_orders ?? {},
      }),
    onMutate: async (orderedProviderIds) => {
      await queryClient.cancelQueries({ queryKey: ['system', 'dashboard-layout'] });
      queryClient.setQueryData(['system', 'dashboard-layout'], (prev: DashboardLayout | undefined) => ({
        provider_order: orderedProviderIds,
        card_orders: prev?.card_orders ?? {},
      }));
    },
    onError: () => {
      toast.error('Could not save provider order');
      queryClient.invalidateQueries({ queryKey: ['system', 'dashboard-layout'] });
    },
  });
  return { configs, layout, saveOrder };
}
