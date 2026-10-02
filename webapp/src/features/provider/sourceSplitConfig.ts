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
  if (kind === 'quota') {
    return {
      title: sourceIsSidecar ? 'Current window by source' : 'Current window by model',
      split: windowSplit,
      useWindowSplit: true,
      windowType: aggregation?.window_type,
      hasSourceSplit: Object.keys(windowSplit).length > 0,
    };
  }

  const split = scopeBucket?.by_model ?? {};
  return {
    title: `Tokens by model · ${scopeLabel}`,
    split,
    useWindowSplit: false,
    windowType: aggregation?.window_type,
    hasSourceSplit: Object.keys(split).length > 0,
  };
}
