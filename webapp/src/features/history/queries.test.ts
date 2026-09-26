import { beforeEach, describe, expect, it } from 'vitest';
import { dateRangeParams } from './queries';
import { setTzConfig } from '@/lib/tz';

beforeEach(() => setTzConfig({ user_timezone: null, env_timezone: null }));

describe('dateRangeParams', () => {
  it('uses local calendar midnights with an exclusive upper bound across DST', () => {
    setTzConfig({ user_timezone: 'Europe/Berlin' });
    expect(dateRangeParams({ since: '2026-03-29', until: '2026-03-29' })).toEqual({
      since: '2026-03-28T23:00:00.000Z',
      until: '2026-03-29T22:00:00.000Z',
    });
  });

  it('keeps preset ranges day-based', () => {
    expect(dateRangeParams({ days: 30 })).toEqual({ days: 30 });
  });
});
