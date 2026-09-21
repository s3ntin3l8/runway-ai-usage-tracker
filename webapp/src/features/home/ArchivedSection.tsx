// Archived providers section — grid of archived cards with lifetime stats.
// Toggled from the home page top bar.

import { useQuery } from '@tanstack/react-query';
import { fetchArchivedProviders } from '@/api/endpoints';
import { useProviderConfigs } from '@/features/home/queries';
import { ArchivedCard } from './ArchivedCard';

export function ArchivedSection() {
  const archived = useQuery({
    queryKey: ['usage', 'archived-providers'],
    queryFn: fetchArchivedProviders,
    refetchInterval: 120_000,
  });
  const configs = useProviderConfigs();

  const items = archived.data?.archived ?? [];
  if (items.length === 0) return null;

  const nameMap = new Map(
    (configs.data?.providers ?? []).map((p) => [p.provider_id, p.name]),
  );

  return (
    <section aria-label="Archived providers" className="flex flex-col gap-2">
      <h2 className="text-xs font-semibold tracking-wide text-fg-subtle uppercase">
        Archived
      </h2>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {items.map((item) => (
          <ArchivedCard
            key={`${item.provider_id}:${item.account_id}`}
            item={item}
            providerName={nameMap.get(item.provider_id) ?? item.provider_id}
          />
        ))}
      </div>
    </section>
  );
}
