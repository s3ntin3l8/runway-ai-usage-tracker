import { useQueryClient } from '@tanstack/react-query';

export const credentialInventoryKey = ['system', 'credentials'] as const;

/**
 * Invalidate every view that reads credential / identity state. Tagging, untagging,
 * assigning, refreshing and removing all change what several screens show (the
 * credential inventory, token-health banners, provider cards, Fleet identities and
 * rules), so a mutation must refresh them together rather than waiting for each poll.
 */
export function useInvalidateCredentialViews() {
  const queryClient = useQueryClient();
  return () => {
    for (const queryKey of [
      credentialInventoryKey,
      ['system', 'token-health'],
      ['system', 'provider-configs'],
      ['fleet'],
      ['usage'],
    ]) {
      queryClient.invalidateQueries({ queryKey });
    }
  };
}
