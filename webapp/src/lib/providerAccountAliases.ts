const PROVIDER_CONFIG_ID_BY_USAGE_PROVIDER: Record<string, string> = {
  'opencode-free': 'opencode',
  'opencode-zen': 'opencode',
  'hermes-auto': 'opencode',
  'hermes-xai-oauth': 'xai',
  'xai-oauth': 'xai',
  'xai-api': 'xai',
};

// Keep tier and oauth aliases in sync with account_config_provider_id in
// app/services/account_identity.py.
/** Return the configured provider_id for a usage provider stream (e.g. tier or oauth aliases). */
export function accountConfigProviderIdForUsage(providerId: string): string {
  return PROVIDER_CONFIG_ID_BY_USAGE_PROVIDER[providerId] ?? providerId;
}
