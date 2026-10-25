// Stable colour slots for the segments.
//
// Colour by SEGMENT, never by position: the visible segment list shrinks
// when "Exclude cache" is toggled and whenever a segment has no value, so a
// positional palette renumbers the colours underneath the user — a colour
// learned yesterday can mean a different segment today.
//
// THE GLOBAL TABLE: SEGMENT_ROLE_SLOT below is the one place that says
// which colour a segment gets. It is keyed by the segment's ROLE — its
// semantic identity — and not by its key or its position, so a segment keeps
// its colour across metrics: "Cache read" is --chart-4 in the token
// breakdown AND in the cost breakdown, and "Reasoning" (tokens only) never
// borrows the colour of a neighbouring segment.
//
// The slot order follows TOKEN_SEGMENT_KEYS (./segmentDefs) — input, output,
// reasoning, cache read, cache create — so the segmented bar, the donut and
// the stacked ranking charts all give a segment the same colour. The cost
// segments reuse those slots through their roles, so the two lists cannot
// drift apart.
//
// This module deliberately imports nothing from echarts: TokenBar renders on
// the eager Home route and must not pull the echarts chunk into its bundle.

import { TOKEN_SEGMENT_KEYS, type SegmentRole } from './segmentDefs';
import type { TokenSliceKey } from './tokenSlices';

/**
 * 1-based `--chart-N` slot owned by each segment role — the single global
 * colour table.
 *
 * Derived from TOKEN_SEGMENT_KEYS so the slot order cannot drift from the
 * order the stacked charts use; the cast is safe because that list covers
 * exactly the five roles.
 */
export const SEGMENT_ROLE_SLOT: Record<SegmentRole, number> = Object.fromEntries(
  TOKEN_SEGMENT_KEYS.map((k, i) => [k.role, i + 1]),
) as Record<SegmentRole, number>;

/**
 * 1-based `--chart-N` slot owned by each token segment.
 *
 * The token-keyed view of the global table, kept for the token charts' API;
 * the cast is safe because that list covers exactly the token slice keys.
 */
export const SEGMENT_COLOR_SLOT: Record<TokenSliceKey, number> = Object.fromEntries(
  TOKEN_SEGMENT_KEYS.map((k) => [k.key, SEGMENT_ROLE_SLOT[k.role]]),
) as Record<TokenSliceKey, number>;

/** The CSS custom property a DOM consumer paints the segment with. */
export function segmentColorVar(key: TokenSliceKey): string {
  return `var(--chart-${SEGMENT_COLOR_SLOT[key]})`;
}

/**
 * Index into the resolved chart palette for a canvas consumer. Canvas cannot
 * resolve `var(--chart-N)`, so ECharts charts need the palette entry itself.
 */
export function segmentPaletteIndex(key: TokenSliceKey): number {
  return SEGMENT_COLOR_SLOT[key] - 1;
}

/** The CSS custom property a DOM consumer paints a segment role with. */
export function roleColorVar(role: SegmentRole): string {
  return `var(--chart-${SEGMENT_ROLE_SLOT[role]})`;
}

/**
 * Index into the resolved chart palette for a canvas consumer, by role.
 */
export function rolePaletteIndex(role: SegmentRole): number {
  return SEGMENT_ROLE_SLOT[role] - 1;
}
