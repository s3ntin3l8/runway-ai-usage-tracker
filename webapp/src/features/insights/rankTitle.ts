// The title for a capped ranking.
//
// A ranking is capped, so a bare "Top models" reads as a complete list when
// it is not one. The title states the cap, and when the ranking was
// truncated it says how much was left out: "Top 12 of 34 models".
//
// Both numbers come from the RESPONSE, not from a constant in this file: the
// API returns the untruncated total and the limit it applied, so the label
// cannot drift from the query that produced it. `total` is undefined until
// the response arrives, and only the cap is known then.

/** "hosts" → "host". Every ranked noun in the app is a regular plural. */
function singular(noun: string): string {
  return noun.endsWith('s') ? noun.slice(0, -1) : noun;
}

export function rankTitle(total: number | undefined, limit: number, noun: string): string {
  // No response yet — the cap is all that is known.
  if (total === undefined) return `Top ${limit} ${noun}`;
  // The ranking was truncated: say how much of it is not on screen.
  if (total > limit) return `Top ${limit} of ${total} ${noun}`;
  // Nothing was left out, so the title can state the real size. An empty
  // ranking has no "top" — "Top 0 models" reads like a broken counter.
  if (total === 0) return `No ${noun}`;
  return `Top ${total} ${total === 1 ? singular(noun) : noun}`;
}
