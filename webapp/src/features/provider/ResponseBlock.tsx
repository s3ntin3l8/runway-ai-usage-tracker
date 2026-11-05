import { useState } from 'react';
import type { StrategyCaptureResponse } from '@/api/types';
import { Badge } from '@/components/ui/Badge';

export function ResponseBlock({ response }: { response: StrategyCaptureResponse }) {
  const [expanded, setExpanded] = useState(false);
  const statusColor =
    response.status < 300 ? 'text-success' : response.status < 500 ? 'text-warning' : 'text-critical';

  return (
    <div className="mb-2 border-l-2 border-border pl-2">
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-2 text-[12px]"
      >
        <Badge variant="neutral">{response.method}</Badge>
        <span className={statusColor}>{response.status}</span>
        <span className="truncate font-mono text-fg-muted">{response.url}</span>
      </button>
      {expanded && (
        <pre className="mt-1 max-h-48 overflow-auto rounded-sm bg-surface-2 p-2 font-mono text-[11px] whitespace-pre-wrap">
          {JSON.stringify(response.body, null, 2)}
        </pre>
      )}
    </div>
  );
}
