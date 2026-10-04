import {
  MAPPING_HINT,
  MAPPING_LABEL,
  STATUS_LABEL,
  STATUS_VARIANT,
  diagnosticsText,
  originSummary,
  relativeExpiry,
} from './display';
import { account, source } from './testData';

describe('credential display helpers', () => {
  it('gives every status a label and a badge variant', () => {
    for (const status of ['valid', 'expiring', 'expired', 'invalid', 'stale', 'unknown']) {
      expect(STATUS_LABEL[status]).toBeTruthy();
      expect(STATUS_VARIANT[status]).toBeTruthy();
    }
    // "Not reported" must not look like a failure, nor like a healthy credential.
    expect(STATUS_VARIANT.stale).toBe('neutral');
    expect(STATUS_VARIANT.invalid).toBe('critical');
  });

  it('describes every mapping kind', () => {
    for (const mapping of Object.keys(MAPPING_LABEL)) {
      expect(MAPPING_HINT[mapping as keyof typeof MAPPING_HINT]).toBeTruthy();
    }
  });

  it('summarises where a credential came from', () => {
    expect(originSummary(source())).toBe('oauth_creds.json on Workstation');
    expect(originSummary(source({ machine_name: null }))).toBe('oauth_creds.json on host-a');
    expect(
      originSummary(source({ origin_kind: 'server', label: 'GITHUB_TOKEN', machine_id: null })),
    ).toBe('GITHUB_TOKEN (server)');
    expect(
      originSummary(source({ origin_kind: 'config', label: 'Manual configuration', machine_id: null })),
    ).toBe('Manual configuration');
  });

  it('formats expiry relative to now', () => {
    expect(relativeExpiry(null)).toBe('no expiry');
    expect(relativeExpiry(undefined)).toBe('no expiry');
    expect(relativeExpiry(30)).toBe('in 1m');
    expect(relativeExpiry(7200)).toBe('in 2h');
    expect(relativeExpiry(3 * 86_400)).toBe('in 3d');
    expect(relativeExpiry(-7200)).toBe('expired 2h ago');
    expect(relativeExpiry(-86_400 * 2)).toBe('expired 2d ago');
  });

  it('builds a diagnostics summary without the raw account id or any secret', () => {
    const text = diagnosticsText(
      'Gemini',
      account(
        [
          source({
            source_id: 'a',
            is_active: true,
            origin_app: 'Gemini CLI',
            machine_name: 'dev-01',
            last_success_at: new Date().toISOString(),
            shared_with: ['mgmt'],
          }),
          source({
            source_id: 'b',
            status: 'expired',
            machine_name: 'mgmt',
            machine_stale: true,
            last_error: 'Authentication failed',
          }),
        ],
        { active_source_id: 'a', status: 'valid', data_source: 'api', input_source: 'sidecar' },
      ),
    );
    expect(text).toContain('Runway credentials — Gemini');
    expect(text).toContain('Credentials (2):');
    expect(text).toContain('Gemini CLI · oauth_creds.json on dev-01');
    expect(text).toContain('same secret on mgmt');
    expect(text).toContain('mgmt (offline)');
    expect(text).toContain('last error: Authentication failed');
    expect(text).toContain('(api / sidecar)');
    expect(text).toContain('Account: a***@example.com');
    expect(text).not.toContain('alice@example.com');
  });
});
