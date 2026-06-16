// In-app Runway mark — a lightweight inline echo of the canonical master
// (assets/logo.svg, see docs/branding.md): a gradient glass capsule read as a
// runway with a dashed white centerline, on a dark disc. The heavy glow/gloss
// filters from the master are dropped so this stays cheap to render at the
// 24px sizes the sidebar and boot gate use.
export function RunwayMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={className} aria-hidden>
      <defs>
        <linearGradient id="runwayMarkPill" x1="12" y1="6.5" x2="12" y2="17.5" gradientUnits="userSpaceOnUse">
          <stop stopColor="#60a5fa" />
          <stop offset="0.5" stopColor="#8b5cf6" />
          <stop offset="1" stopColor="#d946ef" />
        </linearGradient>
      </defs>
      {/* Dark disc base */}
      <circle cx="12" cy="12" r="10.5" fill="#18181b" />
      <circle cx="12" cy="12" r="10.25" stroke="#3f3f46" strokeOpacity="0.6" strokeWidth="0.5" />
      {/* Glass capsule */}
      <rect x="9.9" y="6.5" width="4.2" height="11" rx="2.1" fill="url(#runwayMarkPill)" />
      {/* Runway centerline */}
      <path
        d="M12 8.6V15.4"
        stroke="white"
        strokeOpacity="0.92"
        strokeWidth="0.9"
        strokeLinecap="round"
        strokeDasharray="1.3 1.2"
      />
    </svg>
  );
}
