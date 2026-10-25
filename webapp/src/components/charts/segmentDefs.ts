// Shared segment key/label definitions for stacked bar charts.
// The ranking cards (Top Models, Top Projects, Top Sessions, Top Hosts) all
// build their segment arrays from these — colors and value/accessors are
// applied by the consumer.

/**
 * The semantic identity of a segment, shared by every metric.
 *
 * A segment's ROLE is what owns its colour: "Cache read" is the same segment
 * whether the bar counts tokens or cost, so it has to get the same colour in
 * both. The colour slot of a role lives in the global table
 * (./segmentColors) — the role is the bridge between the two breakdowns.
 */
export type SegmentRole = 'input' | 'output' | 'reasoning' | 'cache_read' | 'cache_create';

export interface SegmentKey {
  key: string;
  label: string;
  /** The semantic identity that owns the colour slot, across metrics. */
  role: SegmentRole;
}

export const TOKEN_SEGMENT_KEYS: SegmentKey[] = [
  { key: 'tokens_input', label: 'Input', role: 'input' },
  { key: 'tokens_output', label: 'Output', role: 'output' },
  { key: 'tokens_reasoning', label: 'Reasoning', role: 'reasoning' },
  { key: 'tokens_cache_read', label: 'Cache read', role: 'cache_read' },
  { key: 'tokens_cache_create', label: 'Cache create', role: 'cache_create' },
];

// The cost breakdown has no reasoning segment of its own (reasoning is billed
// at the output rate and folds into cost_output), but every role it does have
// is a token role — that is what keeps "Cache read" the same colour in both
// breakdowns instead of renumbering the palette under the user.
export const COST_SEGMENT_KEYS: SegmentKey[] = [
  { key: 'cost_input', label: 'Input', role: 'input' },
  { key: 'cost_output', label: 'Output', role: 'output' },
  { key: 'cost_cache_read', label: 'Cache read', role: 'cache_read' },
  { key: 'cost_cache_create', label: 'Cache create', role: 'cache_create' },
];
