// Screen-reader text alternative for the charts.
//
// ECharts renders into a canvas, so everything a chart shows — the segment
// names, the per-point values — exists only as pixels unless the page also
// renders it as text. The components that draw a chart pair it with this
// visually-hidden list, built from the SAME data they hand to ECharts so the
// two can never drift apart.
//
// The list is a SIBLING of the chart, never a child: the chart container
// carries role="img", and an element with role="img" makes its descendants
// presentational — a summary nested inside it would be hidden from assistive
// technology all over again.

export interface AccessibleEntry {
  /** The data point's label (a segment, a model, a day and hour, …). */
  label: string;
  /** The value, already formatted for display. */
  value: string;
  /** Optional context appended to the entry (e.g. a card's subtitle). */
  note?: string;
  /** Optional breakdown of the entry (e.g. the segments of a stacked bar). */
  details?: { label: string; value: string }[];
}

export function AccessibleSummary({
  caption,
  entries,
}: {
  caption: string;
  entries: AccessibleEntry[];
}) {
  return (
    <div className="sr-only">
      <p>{caption}</p>
      {entries.length > 0 ? (
        <ul>
          {entries.map((entry, i) => (
            <li key={`${entry.label}-${i}`}>
              {entry.label}: {entry.value}
              {entry.note ? ` (${entry.note})` : null}
              {entry.details && entry.details.length > 0 ? (
                <ul>
                  {entry.details.map((detail, j) => (
                    <li key={`${detail.label}-${j}`}>
                      {detail.label}: {detail.value}
                    </li>
                  ))}
                </ul>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
