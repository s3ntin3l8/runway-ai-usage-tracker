// Shared data access for the Credentials views.

import { useQuery } from '@tanstack/react-query';
import { fetchCredentialInventory } from '@/api/endpoints';
import { credentialInventoryKey } from '@/hooks/useInvalidateCredentialViews';

export function useCredentialInventory() {
  return useQuery({
    queryKey: credentialInventoryKey,
    queryFn: fetchCredentialInventory,
    refetchInterval: 60_000,
    // Admin-gated: a non-admin session gets a 403 every time, so don't retry it (the
    // Providers badges that read this are best-effort and simply stay neutral).
    retry: false,
  });
}
