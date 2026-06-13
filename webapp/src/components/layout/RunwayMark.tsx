// Minimal runway glyph: converging centerline marks. Inherits currentColor
// so it follows the theme.
export function RunwayMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={className} aria-hidden>
      <rect x="3" y="3" width="18" height="18" rx="5" className="fill-accent" />
      <path
        d="M9.5 17.5 11 6.5M14.5 17.5 13 6.5"
        stroke="white"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeDasharray="2.4 1.6"
      />
    </svg>
  );
}
