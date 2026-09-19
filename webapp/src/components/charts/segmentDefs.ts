// Shared segment key/label definitions for stacked bar charts.
// TopModelsBar and TopProjectsCard both use these to build their
// segment arrays — colors and value/accessors are applied by the consumer.

export interface SegmentKey {
  key: string;
  label: string;
}

export const TOKEN_SEGMENT_KEYS: SegmentKey[] = [
  { key: 'tokens_input', label: 'Input' },
  { key: 'tokens_output', label: 'Output' },
  { key: 'tokens_reasoning', label: 'Reasoning' },
  { key: 'tokens_cache_read', label: 'Cache read' },
  { key: 'tokens_cache_create', label: 'Cache create' },
];

export const COST_SEGMENT_KEYS: SegmentKey[] = [
  { key: 'cost_input', label: 'Input' },
  { key: 'cost_output', label: 'Output' },
  { key: 'cost_cache_read', label: 'Cache read' },
  { key: 'cost_cache_create', label: 'Cache create' },
];
