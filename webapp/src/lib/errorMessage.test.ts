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

  it('falls back when the message says nothing', () => {
    // An empty message is no message. A caller that gates an error state on
    // a truthy string would otherwise fall through to the empty state,
    // which is the failure this helper exists to prevent.
    expect(errorMessage(new Error(''), 'fallback')).toBe('fallback');
    expect(errorMessage({ message: '' }, 'fallback')).toBe('fallback');
    expect(errorMessage({ message: '   ' }, 'fallback')).toBe('fallback');
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
