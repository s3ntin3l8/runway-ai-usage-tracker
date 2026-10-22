import { describe, expect, it } from 'vitest';
import { errorMessage } from './errorMessage';

describe('errorMessage', () => {
  it('returns the message of an Error', () => {
    expect(errorMessage(new Error('boom'))).toBe('boom');
  });

  it('returns the message of an error-like object', () => {
    expect(errorMessage({ message: 'boom' })).toBe('boom');
  });

  it('returns null for a value with no message', () => {
    expect(errorMessage('boom')).toBeNull();
    expect(errorMessage(null)).toBeNull();
    expect(errorMessage(undefined)).toBeNull();
    expect(errorMessage(42)).toBeNull();
    expect(errorMessage({})).toBeNull();
    expect(errorMessage({ message: 42 })).toBeNull();
  });

  it('answers the same for an error from another realm', () => {
    // `instanceof Error` is false for an error created in another realm,
    // which is what a vm test pool sees for a host-created error. Reading
    // the `message` property is not.
    const other = new (class extends Error {})('boom');
    Object.setPrototypeOf(other, Error.prototype);
    expect(errorMessage(other)).toBe('boom');
  });
});
