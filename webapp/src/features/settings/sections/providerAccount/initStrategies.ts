// Strategy rows for the account form: user overrides when present, otherwise
// the provider registry defaults.

import type { CollectionStrategy, ProviderConfig } from '@/api/types';

export interface StrategyEntry {
  id: string;
  enabled: boolean;
  label: string;
}

/**
 * Pick the strategies for the form:
 *   - If the row has explicit `collection_strategies`, prefer those (preserves
 *     user-set overrides).
 *   - Otherwise fall back to `provider.supported_strategies` (registry defaults).
 */
export function initStrategies(provider: ProviderConfig, account: ProviderConfig['accounts'][number]): StrategyEntry[] {
  const src: CollectionStrategy[] =
    account.collection_strategies ?? provider.supported_strategies ?? [];
  return src.map((s) => ({
    id: s.id,
    enabled: s.enabled,
    label: (s as { label?: string }).label ?? s.id,
  }));
}
