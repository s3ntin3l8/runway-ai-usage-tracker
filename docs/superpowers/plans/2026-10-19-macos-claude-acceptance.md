# Complete #672: macOS Claude Native Acceptance

Implement the user-approved plan for Chrome, Safari and Claude Desktop only. Other browsers remain implemented but are excluded from physical acceptance.

1. Add internal per-store scan outcomes and expiry counts, harden Safari parsing/profile discovery, and preserve the old candidate APIs and source identities. Share Desktop OAuth/cookie Safe Storage caching. Never turn failed/empty scans into destructive retirement.
2. Build the production macOS app, use disposable browser profiles and a disposable loopback server/database, verify each source's holder/upstream access and two persisted polls with rediscovery/heartbeat. Exercise native logout and access outcomes separately from synthetic malformed/denial cases. Keep everyday sessions untouched.
3. Record sanitized counts/statuses only; run the full lint/Python/frontend gates, packaging/registry checks and an independent review. Open a Conventional Commits PR and update #672; close it only for evidenced acceptance, with linked blockers for incomplete rows.

No new public endpoint, database migration, production deployment, refresh, credential write or ACL change. Normal browser login/logout and macOS permissions remain user controlled. Keep conservative source retention after empty scans.
