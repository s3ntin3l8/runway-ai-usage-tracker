import { describe, expect, it } from 'vitest';
import { foreignError } from './foreignError';

describe('foreignError', () => {
  it('is not an instanceof Error', () => {
    expect(foreignError('boom') instanceof Error).toBe(false);
  });

  it('keeps the message', () => {
    expect(foreignError('boom').message).toBe('boom');
  });

  it('stringifies like an error', () => {
    expect(String(foreignError('boom'))).toBe('Error: boom');
  });

  it('keeps the stack', () => {
    expect(typeof foreignError('boom').stack).toBe('string');
  });
});
