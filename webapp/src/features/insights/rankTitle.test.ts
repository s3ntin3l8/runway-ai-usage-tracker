import { describe, expect, it } from 'vitest';
import { rankTitle } from './rankTitle';

describe('rankTitle', () => {
  it('states the cap when the total is not known yet', () => {
    // The response has not arrived: the cap is all that can be said.
    expect(rankTitle(undefined, 12, 'models')).toBe('Top 12 models');
  });

  it('says how much of a truncated ranking is missing', () => {
    expect(rankTitle(34, 12, 'models')).toBe('Top 12 of 34 models');
  });

  it('states the real size when nothing was truncated', () => {
    expect(rankTitle(7, 12, 'models')).toBe('Top 7 models');
  });

  it('states the real size when the ranking exactly filled the cap', () => {
    // Twelve groups and a cap of twelve: the list is complete, but it is
    // also exactly at the cap, so the honest title is the plain one.
    expect(rankTitle(12, 12, 'models')).toBe('Top 12 models');
  });

  it('has no "top" for an empty ranking', () => {
    expect(rankTitle(0, 12, 'models')).toBe('No models');
  });

  it('counts one in the singular', () => {
    expect(rankTitle(1, 12, 'hosts')).toBe('Top 1 host');
    expect(rankTitle(1, 12, 'projects')).toBe('Top 1 project');
  });

  it('uses the numbers it is given rather than its own', () => {
    // The cap and the total travel in the response, so the label follows the
    // query even when the query asked for something other than the default.
    expect(rankTitle(3, 2, 'projects')).toBe('Top 2 of 3 projects');
    expect(rankTitle(9, 50, 'sessions')).toBe('Top 9 sessions');
  });
});
