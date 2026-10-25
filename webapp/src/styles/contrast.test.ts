/// <reference types="node" />
// Token-level WCAG contrast guard.
//
// jsdom has no CSS engine, so axe cannot judge colour contrast there — it
// reports the rule as "incomplete" rather than as a violation. This test
// computes the ratios from the token values themselves, so the palette stays
// inside WCAG AA in BOTH themes without needing a browser.
//
// The pairs below are the ones the UI actually renders:
//  - --fg / --fg-muted / --fg-subtle as body, secondary and small (11-12px)
//    text on the canvas and on a card;
//  - the status colours as the 11px badge text on their own -muted tint;
//  - --accent-fg on --accent (the primary button);
//  - white on --critical-strong (the danger button);
//  - the categorical chart colours on a card (graphical objects, 3:1).
//
// The stylesheet is read from disk rather than imported: Vite compiles
// `tokens.css` (the app pulls it in through index.css), and that compiled
// output is not the source of truth for the token values.

import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';

function readTokensCss(): string {
  // vitest may run from the package directory or from the repository root
  // (CI runs it with webapp/ as the working directory, but a developer may
  // not), so look for the file under both layouts.
  let dir = process.cwd();
  for (let i = 0; i < 6; i += 1) {
    for (const relative of ['src/styles/tokens.css', 'webapp/src/styles/tokens.css']) {
      const candidate = resolve(dir, relative);
      if (existsSync(candidate)) return readFileSync(candidate, 'utf8');
    }
    dir = resolve(dir, '..');
  }
  throw new Error('Could not find src/styles/tokens.css');
}

const css = readTokensCss();

interface Theme {
  name: string;
  tokens: Record<string, string>;
}

function parseBlock(selector: string): Record<string, string> {
  const start = css.indexOf(selector);
  if (start === -1) throw new Error(`tokens.css has no ${selector} block`);
  const open = css.indexOf('{', start);
  const close = css.indexOf('}', open);
  const body = css.slice(open + 1, close);
  const tokens: Record<string, string> = {};
  for (const line of body.split(';')) {
    const match = /--([a-z0-9-]+)\s*:\s*([^;]+)/i.exec(line);
    if (match) tokens[match[1]] = match[2].trim();
  }
  return tokens;
}

const themes: Theme[] = [
  { name: 'dark', tokens: parseBlock(':root') },
  { name: 'light', tokens: parseBlock("[data-theme='light']") },
];

/** Parses #rgb, #rrggbb or `rgb(r g b / a)`. */
function parseColor(value: string): { r: number; g: number; b: number; a: number } {
  const v = value.trim();
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(v);
  if (hex) {
    const h = hex[1].length === 3 ? hex[1].replace(/./g, (c) => c + c) : hex[1];
    return {
      r: parseInt(h.slice(0, 2), 16),
      g: parseInt(h.slice(2, 4), 16),
      b: parseInt(h.slice(4, 6), 16),
      a: 1,
    };
  }
  const rgb = /^rgb\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s*\/\s*([\d.]+))?\s*\)$/i.exec(v);
  if (rgb) {
    return {
      r: Number(rgb[1]),
      g: Number(rgb[2]),
      b: Number(rgb[3]),
      a: rgb[4] === undefined ? 1 : Number(rgb[4]),
    };
  }
  throw new Error(`Cannot parse colour: ${value}`);
}

/** Flattens a translucent colour onto an opaque backdrop. */
function composite(
  fg: { r: number; g: number; b: number; a: number },
  bg: { r: number; g: number; b: number },
): { r: number; g: number; b: number } {
  return {
    r: fg.r * fg.a + bg.r * (1 - fg.a),
    g: fg.g * fg.a + bg.g * (1 - fg.a),
    b: fg.b * fg.a + bg.b * (1 - fg.a),
  };
}

function luminance(c: { r: number; g: number; b: number }): number {
  const channel = (raw: number) => {
    const s = raw / 255;
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * channel(c.r) + 0.7152 * channel(c.g) + 0.0722 * channel(c.b);
}

/** WCAG 2.x contrast ratio between two opaque colours. */
export function contrastRatio(
  a: { r: number; g: number; b: number },
  b: { r: number; g: number; b: number },
): number {
  const la = luminance(a);
  const lb = luminance(b);
  const [hi, lo] = la > lb ? [la, lb] : [lb, la];
  return (hi + 0.05) / (lo + 0.05);
}

interface Check {
  label: string;
  fg: string;
  bg: string;
  /** Minimum ratio. Text needs 4.5 (AA normal), graphical objects 3. */
  min: number;
  /** Set when the foreground is a translucent tint laid over the backdrop. */
  tint?: boolean;
}

const CHECKS: Check[] = [
  { label: 'body text on the canvas', fg: 'fg', bg: 'canvas', min: 4.5 },
  { label: 'body text on a card', fg: 'fg', bg: 'surface-1', min: 4.5 },
  { label: 'secondary text on the canvas', fg: 'fg-muted', bg: 'canvas', min: 4.5 },
  { label: 'small labels on a card', fg: 'fg-subtle', bg: 'surface-1', min: 4.5 },
  { label: 'table headers on a card', fg: 'fg-subtle', bg: 'surface-1', min: 4.5 },
  { label: 'small labels on the canvas', fg: 'fg-subtle', bg: 'canvas', min: 4.5 },
  { label: 'accent text on a card', fg: 'accent-text', bg: 'surface-1', min: 4.5 },
  { label: 'accent text on the canvas', fg: 'accent-text', bg: 'canvas', min: 4.5 },
  { label: 'primary button label', fg: 'accent-fg', bg: 'accent', min: 4.5 },
  { label: 'danger button label', fg: 'accent-fg', bg: 'critical-strong', min: 4.5 },
  { label: 'critical badge text', fg: 'critical', bg: 'critical-muted', min: 4.5, tint: true },
  { label: 'warning badge text', fg: 'warning', bg: 'warning-muted', min: 4.5, tint: true },
  { label: 'ok badge text', fg: 'ok', bg: 'ok-muted', min: 4.5, tint: true },
  { label: 'unlimited badge text', fg: 'unlimited', bg: 'unlimited-muted', min: 4.5, tint: true },
  { label: 'unknown badge text', fg: 'unknown', bg: 'unknown-muted', min: 4.5, tint: true },
  { label: 'accent badge text', fg: 'accent-text', bg: 'accent-muted', min: 4.5, tint: true },
  { label: 'chart series 1 on a card', fg: 'chart-1', bg: 'surface-1', min: 3 },
  { label: 'chart series 2 on a card', fg: 'chart-2', bg: 'surface-1', min: 3 },
  { label: 'chart series 3 on a card', fg: 'chart-3', bg: 'surface-1', min: 3 },
  { label: 'chart series 4 on a card', fg: 'chart-4', bg: 'surface-1', min: 3 },
  { label: 'chart series 5 on a card', fg: 'chart-5', bg: 'surface-1', min: 3 },
  { label: 'chart series 6 on a card', fg: 'chart-6', bg: 'surface-1', min: 3 },
];

describe.each(themes)('$name theme contrast', (theme) => {
  const token = (name: string): string => {
    const value = theme.tokens[name];
    if (value === undefined) throw new Error(`${theme.name}: missing --${name}`);
    return value;
  };

  it.each(CHECKS)('$label stays at $min:1 or better', (check) => {
    const card = parseColor(token('surface-1'));
    // A badge paints a translucent TINT as its background and the solid
    // status colour as the text on top of it.
    const backdrop = check.tint
      ? composite(parseColor(token(check.bg)), card)
      : parseColor(token(check.bg));
    const foreground = parseColor(token(check.fg));
    const ratio = contrastRatio(foreground, backdrop);
    expect(
      ratio,
      `${theme.name}: --${check.fg} on --${check.bg} is ${ratio.toFixed(2)}:1 (needs ${check.min}:1)`,
    ).toBeGreaterThanOrEqual(check.min);
  });
});
