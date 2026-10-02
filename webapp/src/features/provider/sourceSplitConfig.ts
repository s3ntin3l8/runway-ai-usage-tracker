import type { CumulativeBucket, CumulativeModelBucket, WindowAggregation } from '@/api/types';
import type { CardKind } from '@/lib/quota';

export interface SourceSplitConfig {
  title: string;
  split: Record<string, CumulativeModelBucket>;
  useWindowSplit: boolean;
  windowType?: string;
  hasSourceSplit: boolean;
}

export function getSourceSplitConfig({
  kind,
  sidecarId,
  aggregation,
  scopeBucket,
  scopeLabel,
}: {
  kind: CardKind;
  sidecarId?: string;
  aggregation?: WindowAggregation;
  scopeBucket: CumulativeBucket | null;
  scopeLabel: string;
}): SourceSplitConfig {
  const bySidecar = aggregation?.by_sidecar ?? {};
  const sourceIsSidecar = Boolean(sidecarId) || Object.keys(bySidecar).length > 1;
  const windowSplit = sidecarId
    ? bySidecar[sidecarId]
      ? { [sidecarId]: bySidecar[sidecarId] }
      : {}
    : sourceIsSidecar
      ? bySidecar
      : (aggregation?.by_model ?? {});
  const useWindowSplit = kind === 'quota';
  const split = useWindowSplit ? windowSplit : (scopeBucket?.by_model ?? {});
  const title = useWindowSplit
    ? sourceIsSidecar
      ? 'Current window by source'
      : 'Current window by model'
    : `Tokens by model · ${scopeLabel}`;

  return {
    title,
    split,
    useWindowSplit,
    windowType: aggregation?.window_type,
    hasSourceSplit: Object.keys(split).length > 0,
  };
}
