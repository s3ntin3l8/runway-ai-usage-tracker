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

### Exact Cowork and Desktop Code tokens

On macOS, Cowork session manifests under
`~/Library/Application Support/Claude/local-agent-mode-sessions/<account>/<org>/`
identify their associated embedded `.claude/projects/**/*.jsonl` transcripts.
The sidecar reads assistant usage from those transcripts with the same Anthropic
parser and event IDs as Claude Code. It preserves event timestamps, session/model
IDs, cache-write TTL splits, entrypoint and app version. Conversation text and raw
records are not uploaded. `audit.jsonl` is excluded because it duplicates transcripts.

Each session's own metadata supplies its account. A stable account/org-root origin
supports the existing pending-event/tag flow when identity is missing; conflicting
session identities are quarantined. The active CLI login is never borrowed.

All available Cowork history is eligible, independently of the CLI timestamp
watermark and `SIDECAR_BOOTSTRAP_DAYS`. Each cycle scans at most 500 assistant
records per current range and normally 4 MiB of transcript data. One complete
record up to 16 MiB may exceed the normal byte budget to ensure progress; larger
records remain pending with a warning. Every scan re-checks the committed prefix
against a fingerprint of its first 4 KiB and last 64 KiB, so the check stays
bounded per transcript instead of re-reading the whole history; a tracked
transcript costs at most another 68 KiB of reads per cycle. A timestamp never
proves a transcript is unchanged: a rewrite in place inside one filesystem
timestamp tick keeps the file's mtime, so the fingerprint — not the clock —
decides whether the reader resumes at the committed offset or re-reads the file.
Processing starts with the oldest modified
files. Separate per-file offsets track only complete lines, detect replacement or
truncation or in-place rewrites, and advance after accepted ingestion or a synchronized offline queue
write. A crash before checkpointing replays through server event deduplication.
An upgrade that changes the recorded fingerprint re-reads each transcript once;
the replay is deduplicated by the server.
Queued events retain their account binding. A changed known account binding
quarantines the file rather than relabeling its history. State lives beside the
sidecar config in `cowork-progress.json`.

Desktop Code usually writes ordinary Claude Code project logs and is collected by
the existing path. On the investigated Mac, 83 of 86 Desktop Code session manifests
matched those logs; the unmatched manifests do not establish missing token usage.
No parallel Desktop Code collector is added. Three unmatched manifests (two with
completed turns) need a separate source/lifecycle investigation tracked in #665.

Ordinary Desktop Chat token accounting is not established. The inspected Desktop
2.42304.0 includes agent usage-ledger code, but no real ledger files were found;
that format is not ingested. Quota reporting and cookie access do not establish
per-chat token coverage.

### Disable Desktop credential access

Set `RUNWAY_CLAUDE_DESKTOP_CREDENTIALS=off` in the sidecar process environment to
skip the Desktop cookie store and its Keychain lookup. Browser credential discovery
and exact local token collection remain available. Set the variable in launchd's
`EnvironmentVariables` before restarting a launchd-managed sidecar; an interactive
shell export does not configure a separately launched tray application. This does
not suppress Keychain access required by other credential sources.

Tracking: #655 (tray), #656 (Cowork and Desktop Code), #662 (ordinary Chat tokens),
#663 (macOS credential inventory/fallback), #665 (unmatched Desktop Code sources).
Quota collection stays server-side.
