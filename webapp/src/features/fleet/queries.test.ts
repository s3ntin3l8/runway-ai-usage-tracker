import type { Sidecar } from '@/api/types';
import {
  buildSidecarNameMap,
  relabelSidecarSplit,
  sidecarDisplayName,
  sidecarLabel,
} from './queries';

const sc = (o: Partial<Sidecar> = {}): Sidecar => ({ sidecar_id: 'id-1', ...o });

describe('sidecarDisplayName', () => {
  it('prefers custom_name over hostname and id', () => {
    expect(sidecarDisplayName(sc({ custom_name: 'Laptop', hostname: 'host-01' }))).toBe('Laptop');
  });

  it('falls back to hostname when there is no custom name', () => {
    expect(sidecarDisplayName(sc({ custom_name: null, hostname: 'host-01' }))).toBe('host-01');
  });

  it('falls back to the raw sidecar_id when nothing else is set', () => {
    expect(sidecarDisplayName(sc({ sidecar_id: 'raw-id' }))).toBe('raw-id');
  });
});

describe('buildSidecarNameMap', () => {
  it('maps each sidecar_id to its display name', () => {
    const map = buildSidecarNameMap([
      sc({ sidecar_id: 'a', custom_name: 'Alpha' }),
      sc({ sidecar_id: 'b', hostname: 'beta-host' }),
    ]);
    expect(map.get('a')).toBe('Alpha');
    expect(map.get('b')).toBe('beta-host');
    expect(map.size).toBe(2);
  });
});

describe('sidecarLabel', () => {
  const names = new Map([['device-720f1366', 'desktop']]);

  it('uses the registry name', () => {
    expect(sidecarLabel(names, 'device-720f1366')).toBe('desktop');
  });

  it('calls the server\'s own collectors "This server"', () => {
    expect(sidecarLabel(names, 'local')).toBe('This server');
  });

  it('keeps the raw id for a sidecar the registry no longer knows', () => {
    expect(sidecarLabel(names, 'gone-host')).toBe('gone-host');
  });
});

describe('relabelSidecarSplit', () => {
  const bucket = (n: number) => ({ tokens_input: n });

  it('re-keys a split from sidecar ids to names, keeping the values', () => {
    const names = new Map([
      ['device-720f13661a254f1e9a6691e8aba11158', 'desktop'],
      ['dev-01', 'dev-01'],
    ]);

    const out = relabelSidecarSplit(
      { 'device-720f13661a254f1e9a6691e8aba11158': bucket(5), 'dev-01': bucket(7), local: bucket(1) },
      names,
    );

    expect(out).toEqual({ desktop: bucket(5), 'dev-01': bucket(7), 'This server': bucket(1) });
  });

  it('keeps two entries with the same name apart instead of merging them', () => {
    const names = new Map([
      ['macbook', 'macbook'],
      ['device-38e9b7176a114cafb432cd7b613b153d', 'macbook'],
    ]);

    const out = relabelSidecarSplit(
      { macbook: bucket(3), 'device-38e9b7176a114cafb432cd7b613b153d': bucket(4) },
      names,
    );

    expect(Object.keys(out).sort()).toEqual(['macbook (38e9)', 'macbook (macb)']);
    expect(Object.values(out).map((b) => b.tokens_input).sort()).toEqual([3, 4]);
  });

  it('returns an empty split unchanged', () => {
    expect(relabelSidecarSplit({}, new Map())).toEqual({});
  });
});
