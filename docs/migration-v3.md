# Migration notes for the next major version

## Provider setup and configuration

The provider settings page now uses the multi-account provider grid by default.
The former single-account overview and edit dialog have been retired. Existing
provider configuration remains available in the grid, including the `default`
account.

Provider configuration updates now require an explicit account ID. Update
scripts and integrations that used:

```http
PUT /api/v1/system/provider-config/openrouter
```

to include the account ID:

```http
PUT /api/v1/system/provider-config/openrouter/default
```

For named accounts, use their stable account ID in the final path segment,
for example `/provider-config/anthropic/alice@example.com`. URL-encode path
segments in clients. The old one-segment `PUT /provider-config/{provider_id}`
shortcut is removed; all writes must identify the account being changed.

The per-account API also avoids ambiguity for providers with multiple saved
credentials. A first credential normally belongs to `default`; use the
provider's account ID for credentials already associated with an identity.
