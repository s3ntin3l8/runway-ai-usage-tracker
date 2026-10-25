// Stable colour slots for the token segments.
//
// Colour by SEGMENT KEY, never by position: the visible segment list shrinks
// when "Exclude cache" is toggled and whenever a segment has no value, so a
// positional palette renumbers the colours underneath the user — a colour
// learned yesterday can mean a different segment today.
//
// The slot order follows TOKEN_SEGMENT_KEYS (./segmentDefs) — input, output,
// reasoning, cache read, cache create — so the segmented bar, the donut and
// the stacked ranking charts all give a segment the same colour.
//
// This module deliberately imports nothing from echarts: TokenBar renders on
// the eager Home route and must not pull the echarts chunk into its bundle.

import { TOKEN_SEGMENT_KEYS } from './segmentDefs';
import type { TokenSliceKey } from './tokenSlices';

/**
 * 1-based `--chart-N` slot owned by each token segment.
 *
 * Derived from TOKEN_SEGMENT_KEYS so the slot order cannot drift from the
 * order the stacked charts use; the cast is safe because that list covers
 * exactly the token slice keys.
 */
export const SEGMENT_COLOR_SLOT: Record<TokenSliceKey, number> = Object.fromEntries(
  TOKEN_SEGMENT_KEYS.map((k, i) => [k.key, i + 1]),
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
