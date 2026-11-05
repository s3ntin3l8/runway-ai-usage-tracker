import { TriangleAlert } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { sidecarLabel } from './queries';

export function UntaggedBanner({
  counts,
  names,
  loading,
  onResolveAll,
  onResolveSidecar,
}: {
  counts: Record<string, number>;
  /** sidecar_id → display name */
  names: Map<string, string>;
  loading: boolean;
  onResolveAll: () => void;
  /** Open the resolver for all of one sidecar's untagged credentials. */
  onResolveSidecar: (sidecarId: string) => void;
}) {
  // Banner is hidden when nothing pending — same render path either way
  // so the parent doesn't have to conditionalize.
  const totalCount = Object.values(counts).reduce((acc, n) => acc + n, 0);
  if (totalCount === 0 && !loading) return null;

  return (
    <Card className="mb-3 border-warning/40 bg-warning-muted p-3">
      <div className="flex items-start gap-3">
        <TriangleAlert
          className="mt-0.5 size-4 shrink-0 text-warning"
          aria-hidden
        />
        <div className="min-w-0 flex-1">
          <p className="text-[13px] font-semibold">
            Untagged credentials
            <span className="ml-2 font-mono text-[11px] text-fg-muted">
              {totalCount} pending
            </span>
          </p>
          <p className="mt-0.5 text-[11px] text-fg-muted">
            Sidecars are blocking these from upload until you map each to a configured provider row.
          </p>

          {Object.keys(counts).length > 0 ? (
            <ul className="mt-2 flex flex-wrap gap-1">
              {Object.entries(counts).map(([sidecarId, count]) => {
                return (
                  <li key={sidecarId}>
                    <button
                      type="button"
                      onClick={() => onResolveSidecar(sidecarId)}
                      className="rounded-md border border-warning/40 bg-surface-1 px-2 py-0.5 text-[11px] font-medium text-fg hover:border-warning"
                      aria-label={`${count} untagged credential${
                        count === 1 ? '' : 's'
                      } on sidecar ${sidecarLabel(names, sidecarId)} — click to resolve`}
                      title={`${sidecarLabel(names, sidecarId)}: ${count} pending`}
                    >
                      <span className="font-mono">{sidecarLabel(names, sidecarId)}</span> · {count}
                    </button>
                  </li>
                );
              })}
            </ul>
          ) : null}
        </div>

        <Button size="sm" variant="primary" onClick={onResolveAll}>
          Tag now
        </Button>
      </div>
    </Card>
  );
}
