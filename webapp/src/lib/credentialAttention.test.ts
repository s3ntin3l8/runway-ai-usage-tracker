import { describe, expect, it } from 'vitest';
import { credentialsNeedingAttention } from './credentialAttention';
import { account, inventory, source } from '@/features/settings/sections/credentials/testData';

const inv = (sources: ReturnType<typeof source>[], accountId = 'alice@example.com', label: string | null = null) =>
  inventory({
    providers: [
      {
        provider_id: 'gemini',
        name: 'Gemini',
        accounts: [account(sources, { account_id: accountId, account_label: label })],
      },
    ],
  });

describe('credentialsNeedingAttention', () => {
  it('returns nothing without an inventory (a non-admin 403)', () => {
    expect(credentialsNeedingAttention(undefined)).toEqual([]);
  });

  it.each(['expired', 'expiring', 'invalid'])('flags a %s credential', (status) => {
    expect(credentialsNeedingAttention(inv([source({ status })]))).toEqual([
      { provider: 'gemini', accountName: 'alice@example.com', status },
    ]);
  });

  it.each(['valid', 'stale', 'unknown'])('ignores a %s credential', (status) => {
    expect(credentialsNeedingAttention(inv([source({ status })]))).toEqual([]);
  });

  it('ignores a redundant, disabled or unused credential', () => {
    const dead = { status: 'expired' };
    expect(credentialsNeedingAttention(inv([source({ ...dead, redundant: true })]))).toEqual([]);
    expect(credentialsNeedingAttention(inv([source({ ...dead, enabled: false })]))).toEqual([]);
    expect(
      credentialsNeedingAttention(inv([source({ ...dead, unused_reason: 'shadowed_by_config_key' })])),
    ).toEqual([]);
  });

  it('names the account: label, server environment, default account, else the masked id', () => {
    const dead = { status: 'invalid' };
    expect(credentialsNeedingAttention(inv([source(dead)], 'a@x.com', ' Work '))[0].accountName).toBe('Work');
    expect(
      credentialsNeedingAttention(inv([source({ ...dead, origin_kind: 'server' })], 'default'))[0].accountName,
    ).toBe('Server environment');
    expect(credentialsNeedingAttention(inv([source(dead)], 'default'))[0].accountName).toBe('Default account');
    const hash = '72ca8b0011223344556677889900aabbccddeeff00112233445566778899a9f5'; // pragma: allowlist secret
    expect(credentialsNeedingAttention(inv([source(dead)], hash))[0].accountName).toBe('72ca8b00…a9f5');
  });
});
