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

  it('returns null when no label AND displayAccountName already shows the account_id (no duplicate)', () => {
    // Both the title and the subtitle would render "bob@example.com" — that's a
    // duplicate. Suppress the subtitle so the row isn't visually busy.
    expect(accountSubtitle({ account_id: 'bob@example.com', account_label: null })).toBeNull();
  });

  it('returns null when no label AND account_id looks like a UUID (display name already covers it)', () => {
    expect(
      accountSubtitle({ account_id: '58235613-1234-1234-1234-123456789012', account_label: null }),
    ).toBeNull();
  });

  it('returns null when no label AND account_id looks like a SHA hash (display name already covers it)', () => {
    expect(
      accountSubtitle({
        account_id: 'e5e9fa1ba31ecd1ae84f75caaa474f3a663f05f4',
        account_label: null,
      }),
    ).toBeNull();
  });

  it('returns null when label equals account_id (no useful secondary line)', () => {
    expect(accountSubtitle({ account_id: 'alice@example.com', account_label: 'alice@example.com' })).toBeNull();
  });
});
