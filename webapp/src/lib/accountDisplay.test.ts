import { accountSubtitle, displayAccountName } from './accountDisplay';

describe('displayAccountName', () => {
  it('returns the trimmed label when present', () => {
    expect(displayAccountName({ account_id: 'alice@example.com', account_label: 'Work' })).toBe(
      'Work',
    );
  });

  it('trims whitespace-only labels to fall through', () => {
    expect(displayAccountName({ account_id: 'alice@example.com', account_label: '   ' })).toBe(
      'alice@example.com',
    );
  });

  it('returns "Default account" for the default account_id with no label', () => {
    expect(displayAccountName({ account_id: 'default', account_label: null })).toBe(
      'Default account',
    );
  });

  it('falls back to account_id when label is null and id is not "default"', () => {
    expect(displayAccountName({ account_id: 'alice@example.com', account_label: null })).toBe(
      'alice@example.com',
    );
  });
});

describe('accountSubtitle', () => {
  it('returns null for the default account', () => {
    expect(accountSubtitle({ account_id: 'default', account_label: null })).toBeNull();
  });

  it('returns the account_id when label differs', () => {
    expect(accountSubtitle({ account_id: 'alice@example.com', account_label: 'Work' })).toBe(
      'alice@example.com',
    );
  });

  it('returns null for an email-shaped account_id when there is no label (display name already shows it)', () => {
    // displayAccountName falls back to the account_id verbatim when the
    // label is empty, so the subtitle would be a duplicate. Suppressed
    // regardless of the id shape — the regex branches the early Hermes
    // review flagged as dead code are gone (#294 S1 / #295 follow-up).
    expect(accountSubtitle({ account_id: 'bob@example.com', account_label: null })).toBeNull();
  });

  it('returns null for a UUID account_id when there is no label (display name already shows it)', () => {
    expect(
      accountSubtitle({ account_id: '58235613-1234-1234-1234-123456789012', account_label: null }),
    ).toBeNull();
  });

  it('returns null for a SHA-hash account_id when there is no label (display name already shows it)', () => {
    expect(
      accountSubtitle({
        account_id: 'e5e9fa1ba31ecd1ae84f75caaa474f3a663f05f4', // pragma: allowlist secret
        account_label: null,
      }),
    ).toBeNull();
  });

  it('returns null when label equals account_id (no useful secondary line)', () => {
    expect(accountSubtitle({ account_id: 'alice@example.com', account_label: 'alice@example.com' })).toBeNull();
  });

  it('returns null for opaque short account_ids (display name already covers them)', () => {
    // displayAccountName falls back to account_id when label is empty AND
    // id is not "default", so the subtitle would be a duplicate string —
    // suppress it regardless of whether the id looks like an email/uuid/hash.
    expect(accountSubtitle({ account_id: 'short-opaque', account_label: null })).toBeNull();
  });

  it('treats a whitespace-only label as missing for email-shaped ids', () => {
    // Whitespace-only labels are stripped (same as displayAccountName). The
    // resulting empty label then takes the no-label branch: the subtitle
    // is suppressed because displayAccountName already shows the
    // account_id verbatim.
    expect(
      accountSubtitle({ account_id: 'bob@example.com', account_label: '   ' }),
    ).toBeNull();
  });
});
