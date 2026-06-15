// Right-aligned "Exclude cache" switch, wired to the shared useExcludeCache
// preference. Mounts on every surface that shows token aggregates (Home,
// Overview, Activity, Cost); only one mounts per route so the shared id is fine.
import { useExcludeCache } from '@/hooks/useExcludeCache';
import { Switch } from '@/components/ui/Switch';

export function ExcludeCacheToggle({ className }: { className?: string }) {
  const { excludeCache, setExcludeCache } = useExcludeCache();
  return (
    <div className={className ?? 'flex items-center justify-end gap-2'}>
      <label htmlFor="exclude-cache" className="text-[12px] text-fg-muted">
        Exclude cache
      </label>
      <Switch id="exclude-cache" checked={excludeCache} onCheckedChange={setExcludeCache} />
    </div>
  );
}
