const PROVIDER_CONFIG_ID_BY_USAGE_PROVIDER: Record<string, string> = {
  'opencode-free': 'opencode',
  'opencode-zen': 'opencode',
};

// Keep tier aliases in sync with account_config_provider_id in
// app/services/account_identity.py.
/** Return the account-config provider for an OpenCode usage stream. */
export function accountConfigProviderIdForUsage(providerId: string): string {
  return PROVIDER_CONFIG_ID_BY_USAGE_PROVIDER[providerId] ?? providerId;
}
