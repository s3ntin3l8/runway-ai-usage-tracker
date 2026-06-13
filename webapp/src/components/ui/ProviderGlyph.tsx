// Provider mark: the real brand logo on a white "app-icon" chip when we vendor
// one (see providerIcons.ts), else a deterministic colored monogram tile so
// unknown providers still get a stable, distinct glyph.

import { cn } from '@/lib/cn';
import { providerIconUrl } from './providerIcons';

const TILE_COLORS = [
  'bg-chart-1',
  'bg-chart-2',
  'bg-chart-3',
  'bg-chart-4',
  'bg-chart-5',
  'bg-chart-6',
];

function hash(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}

interface ProviderGlyphProps {
  providerId: string;
  name?: string;
  className?: string;
}

export function ProviderGlyph({ providerId, name, className }: ProviderGlyphProps) {
  const iconUrl = providerIconUrl(providerId);

  if (iconUrl) {
    return (
      <span
        aria-hidden
        className={cn(
          'inline-flex size-7 shrink-0 items-center justify-center overflow-hidden rounded-md bg-white ring-1 ring-black/10',
          className,
        )}
      >
        <img src={iconUrl} alt="" className="size-[72%] object-contain" loading="lazy" />
      </span>
    );
  }

  const label = (name || providerId).trim();
  const monogram = label.slice(0, 2).toUpperCase();
  const color = TILE_COLORS[hash(providerId) % TILE_COLORS.length];
  return (
    <span
      aria-hidden
      className={cn(
        'inline-flex size-7 shrink-0 items-center justify-center rounded-sm text-[11px] font-bold text-white',
        color,
        className,
      )}
    >
      {monogram}
    </span>
  );
}
