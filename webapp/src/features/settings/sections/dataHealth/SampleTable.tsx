// Renders a check's `samples` — a small, secret-safe whitelist of fields
// the server chose to surface (see app/services/data_health/base.py's
// `Finding`), never a raw row. Detail values are already JSON primitives.

import type { DataHealthFinding } from '@/api/types';

export function SampleTable({ samples }: { samples: DataHealthFinding[] }) {
  if (samples.length === 0) return null;

  return (
    <div className="flex flex-col gap-1.5 rounded-md border border-edge bg-surface-1 p-2.5">
      {samples.map((sample, i) => (
        <div key={`${sample.label}-${i}`} className="flex flex-col gap-0.5">
          <span className="text-[12px] font-medium text-fg">{sample.label}</span>
          {Object.keys(sample.detail).length > 0 && (
            <dl className="flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-fg-subtle">
              {Object.entries(sample.detail).map(([key, value]) => (
                <div key={key} className="flex gap-1">
                  <dt className="font-medium">{key}:</dt>
                  <dd className="font-mono">{formatDetailValue(value)}</dd>
                </div>
              ))}
            </dl>
          )}
        </div>
      ))}
    </div>
  );
}

function formatDetailValue(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}
