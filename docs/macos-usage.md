# macOS usage collection

## Tray sync and upload counters

The tray separates the last successful server sync (including an idle heartbeat)
from the last successful data upload. The metric and event counts belong to that
upload, not to the process lifetime and not to a token total. Idle heartbeats and
failed uploads do not replace them. Successful offline-queue replay updates the
last-upload time and counts for the last acknowledged replayed payload. A server-side event-storage error does not
count as successful event delivery.

## Claude Desktop and Claude Code

Claude Code usage events come from its project JSONL logs. Claude Desktop chat
activity does not necessarily appear there. On macOS the sidecar can read the
Desktop application's `~/Library/Application Support/Claude/Cookies` store and
its `Claude Safe Storage` Keychain item. It forwards the session cookie through
the existing credential pipeline; the server verifies account identity and uses
the existing Claude web quota collector. Existing server freshness and stale
cache handling apply. The sidecar does not refresh or write the Desktop login.

Only the `sessionKey` cookie for `claude.ai` is read from this app-owned store.
Expired cookies are ignored. Chromium v24 host binding and PKCS7 padding are
validated before forwarding a value. Keychain successes and failures are cached
in memory for ten minutes to avoid a Keychain lookup on every provider poll.
Warnings report the failed stage without cookie values or Keychain output.
For `claude.ai` sessionKey, Desktop is tried before browser stores. A successful
read logs its source (Desktop or browser name), without the cookie value. If the
Desktop account differs from a browser account, this precedence selects the
Desktop cookie for server identity verification. If Desktop credentials or an
optional helper/crypto dependency are unavailable, browser sources remain available.
Keychain access may require macOS permission; no ACLs are changed automatically.

### Quotas are not token events

Claude Desktop's `plan-usage-history.json` contains sampled quota utilization:
`u.fh` is the five-hour utilization and `u.sd` is the seven-day utilization;
`t` is the sample time in epoch milliseconds and `org` identifies the organization.
These percentages are not token counts. The sidecar does not turn those samples
into message events or stamp old samples as fresh usage. This avoids duplicate
Claude Code events and misleading token totals.

The inspected Claude Desktop 2.42304.0 app code also supports a per-turn
`usage-ledger/*.ndjson` format with model token counters for chat, cowork and code
surfaces. No ledger files were found in this Mac's Claude application data or
Claude Code directories during investigation. Exact Desktop token accounting is
therefore not implemented by this change; it requires real source fixtures,
identity attribution and deduplication against Claude Code before ingestion.

Tracking: #655 (tray), #656 (Desktop coverage).
