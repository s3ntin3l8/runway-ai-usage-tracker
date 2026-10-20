import type { ProviderConfig } from '@/api/types';

export function providerFamilyId(providerId: string): string {
  return providerId === 'anthropic_console' ? 'anthropic' : providerId;
}

export function claudeConnectionLabel(providerId: string): string {
  return providerId === 'anthropic_console' ? 'Console API credits' : 'Subscription quotas';
}

/** Display-only grouping. Always use the original provider IDs for mutations. */
export function groupProviderFamilies(providers: ProviderConfig[]): ProviderConfig[] {
  const claude = providers.filter((p) => providerFamilyId(p.provider_id) === 'anthropic');
  if (claude.length < 2) return providers;
  const representative = claude.find((p) => p.provider_id === 'anthropic') ?? claude[0];
  const grouped: ProviderConfig = {
    ...representative,
    provider_id: 'anthropic',
    name: 'Claude',
    accounts: claude.flatMap((p) => p.accounts),
    account_count: claude.reduce((n, p) => n + p.account_count, 0),
    archived_count: claude.reduce((n, p) => n + (p.archived_count ?? 0), 0),
  };
  let inserted = false;
  return providers.flatMap((p) => {
    if (providerFamilyId(p.provider_id) !== 'anthropic') return [p];
    if (inserted) return [];
    inserted = true;
    return [grouped];
  });
}
