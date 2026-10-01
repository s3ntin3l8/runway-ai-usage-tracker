// Shared data access for the Credentials views.

import { useQuery } from '@tanstack/react-query';
import { fetchCredentialInventory } from '@/api/endpoints';
import { credentialInventoryKey } from '@/hooks/useInvalidateCredentialViews';

export function useCredentialInventory() {
  return useQuery({
    queryKey: credentialInventoryKey,
    queryFn: fetchCredentialInventory,
    refetchInterval: 60_000,
  });
}
