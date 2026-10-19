# Claude Desktop OAuth and Claude Code Credential Discovery

Approved specification: implement both paths in one PR, connecting all verified accounts.
Tracking: #679 Desktop OAuth, #673 Code service discovery; #672 remaining native stores and #526 renewal stay separate.

## Tasks
1. Read-only sidecar extraction: two Desktop caches, shared Safe Storage key, 30-second Desktop lookup, denial suppression, no refresh export, Code service/account/config-dir discovery and expiry.
2. Source-pinned server profile and quota verification, cache-account binding, exact-generation guards, OAuth preference and fallback, clear origin labels and existing Desktop opt-out.
3. Synthetic extraction, ingestion, rotation, account-isolation and lifecycle tests; native disposable signed ingestion/two polls/heartbeat.
4. Full repository gate, packaging/registry consistency, independent whole-branch review and Conventional Commits PR; close only evidenced acceptance.

## Constraints
No credential writes, refresh requests, ACL modifications or production deployment. Server never reads local credential files. Preserve last-good sources when access fails. Tests block native credential-store access.
