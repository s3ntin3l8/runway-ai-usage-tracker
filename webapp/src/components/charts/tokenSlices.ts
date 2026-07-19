// Token composition slice definitions, shared between TokenDonut (ECharts
// pie) and TokenBar (CSS segmented bar) so the two read identically — same
// order, labels, and cache handling.
//
// Deliberately has ZERO import of EChart/echarts. TokenBar renders on the
// eager Home route and previously pulled these constants straight out of
// TokenDonut.tsx, which statically imports echarts/core — that alone dragged
// the ~685KB echarts chunk into Home's critical-path bundle even though Home
// renders no ECharts chart. Keep this file chart-free.

export type TokenSliceKey =
  | 'tokens_input'
  | 'tokens_output'
  | 'tokens_cache_read'
  | 'tokens_cache_create'
  | 'tokens_reasoning';

export const SLICES: { key: TokenSliceKey; label: string }[] = [
  { key: 'tokens_input', label: 'Input' },
  { key: 'tokens_output', label: 'Output' },
  { key: 'tokens_cache_read', label: 'Cache read' },
  { key: 'tokens_cache_create', label: 'Cache create' },
  { key: 'tokens_reasoning', label: 'Reasoning' },
];

export const CACHE_KEYS = new Set<TokenSliceKey>(['tokens_cache_read', 'tokens_cache_create']);
