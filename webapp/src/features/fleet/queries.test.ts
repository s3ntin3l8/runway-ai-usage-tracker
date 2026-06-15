import type { Sidecar } from '@/api/types';
import { buildSidecarNameMap, sidecarDisplayName } from './queries';

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
