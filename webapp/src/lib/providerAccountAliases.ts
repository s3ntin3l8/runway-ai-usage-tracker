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

// Usage streams whose events may also be assigned to an account of another
// provider (gemini-cli is gone; Google's subscription now surfaces through
// Antigravity). Keep in sync with _RELATED_ACCOUNT_PROVIDERS in
// app/services/account_identity.py.
const RELATED_ACCOUNT_PROVIDERS: Record<string, string[]> = {
  gemini: ['antigravity'],
};

/** Return other providers whose accounts may own this provider's usage. */
export function relatedAccountProviderIds(configProviderId: string): string[] {
  return RELATED_ACCOUNT_PROVIDERS[configProviderId] ?? [];
}
