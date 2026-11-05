// One draggable credential-source row. Secrets are never rendered; the parent
// owns enabled state and rank.

import { useSortable } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { GripVertical } from 'lucide-react';
import type { CredentialSourceSummary } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Switch } from '@/components/ui/Switch';
import { formatLocalDateTime } from '@/lib/tz';

export function SortableCredentialSourceRow({
  source,
  machineName,
  onToggle,
}: {
  source: CredentialSourceSummary;
  /** Display name of the reporting machine; falls back to the raw sidecar id. */
  machineName?: string;
  onToggle: (enabled: boolean) => void;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: source.source_id,
  });
  // A cookie is a browser session, not a file like auth.json — say so, so the
  // row can't be read as another credential file.
  const isCookie = source.source_type === 'cookie';
  const availability = source.available
    ? source.health === 'auth_failed'
      ? 'Authentication failed'
      : source.health === 'degraded'
        ? 'Quota collected; some requests were rejected'
        : source.health === 'untried'
          ? 'Not yet tried'
          : 'Available'
    : source.source_type === 'config'
      ? 'Manual configuration is not currently available'
      : source.sidecar_id
        ? 'Sidecar unavailable or credential expired'
        : 'Unavailable · awaiting refresh';
  return (
    <li
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      className={`flex items-center gap-2 py-2 ${isDragging ? 'z-10 opacity-60' : ''}`}
    >
      <button
        type="button"
        {...attributes}
        {...listeners}
        className="touch-none rounded p-1 text-fg-muted"
        aria-label={`Reorder ${source.source_label || source.source_type}`}
      >
        <GripVertical className="size-3.5" aria-hidden />
      </button>
      <div className="min-w-0 flex-1">
        <p className="truncate text-[12px] font-medium">
          {isCookie ? (
            <Badge variant="outline" className="mr-1">
              Cookie
            </Badge>
          ) : null}
          {source.source_label || source.source_type}
          {source.sidecar_id ? (
            <span className="ml-1 font-normal text-fg-subtle">
              · {machineName ?? source.sidecar_id}
            </span>
          ) : null}
        </p>
        <p className="text-[11px] text-fg-subtle">
          {[
            isCookie ? 'Browser session cookie — not a file' : null,
            source.source_detail,
            availability,
            source.last_seen ? `seen ${formatLocalDateTime(source.last_seen)}` : null,
          ]
            .filter(Boolean)
            .join(' · ')}
        </p>
      </div>
      <Switch
        checked={source.enabled}
        onCheckedChange={onToggle}
        aria-label={`Enable ${source.source_label || source.source_type}`}
      />
    </li>
  );
}
