import { describe, expect, it } from 'vitest';
import { errorMessage } from './errorMessage';
import { foreignError } from '@/test/foreignError';

describe('errorMessage', () => {
  it('returns the message of an Error', () => {
    expect(errorMessage(new Error('boom'), 'fallback')).toBe('boom');
  });

  it('returns the message of an error-like object', () => {
    expect(errorMessage({ message: 'boom' }, 'fallback')).toBe('boom');
  });

  it('falls back when the value carries no message', () => {
    expect(errorMessage('boom', 'fallback')).toBe('fallback');
    expect(errorMessage(null, 'fallback')).toBe('fallback');
    expect(errorMessage(undefined, 'fallback')).toBe('fallback');
    expect(errorMessage(42, 'fallback')).toBe('fallback');
    expect(errorMessage({}, 'fallback')).toBe('fallback');
    expect(errorMessage({ message: 42 }, 'fallback')).toBe('fallback');
  });

  it('answers the same for an error from another realm', () => {
    // The premise, asserted: `instanceof Error` is false for an error
    // created in another realm, which is what a vm test pool sees for a
    // host-created error. Reading the `message` property is not affected.
    const other = foreignError('boom');
    expect(other instanceof Error).toBe(false);
    expect(errorMessage(other, 'fallback')).toBe('boom');
  });
});
