# Data Health

Settings → Data health runs a fixed set of read-only checks over the
database and, for each one that finds something, offers an in-app fix —
preview first, then apply. The checks live in
`app/services/data_health/checks/` and each one owns its own `detect()` and
its own repair. Most of them (17 of the 20) reuse the shared repair helpers
in `app/services/maintenance/`; the rest — `alert_channels`,
`orphan_credential_tags` and `pending_events` — implement their fix
in-place. See [api-reference.md](api-reference.md) for the underlying routes
and [maintenance-scripts.md](maintenance-scripts.md) for the equivalent
command-line repair scripts.

Each category is shown with a plain-language name and a short explanation.
Expand it to see the likely impact, a suggested next step, and any dependency
that must be resolved first. Counts summarize findings, fixable items, and
checks waiting on another repair. Pending events include a direct link to Fleet.

## How a fix works

1. **Scan.** Opening the page (or `POST /rescan`) runs every check's
   read-only `detect()` against the current database. The report is cached
   — it does not re-scan on every page load — and refreshes automatically
   after any fix completes.
2. **Preview.** Each finding is grouped into independently-fixable slices
   (e.g. one group per legacy provider id, one per stray account). A
   fixable group's **Fix** button opens a dialog with any parameters the
   fix needs (a target account, an action to take) and a **Preview**
   button that shows what would change without writing anything.
3. **Apply.** Apply only unlocks once you've previewed with the exact
   parameters you're about to apply, plus an explicit confirmation. It
   returns immediately with a job id; the dialog polls that job until it
   finishes and shows the result.

A check with unresolved findings can block another check from being
applied until it's fixed — the dependency order below reflects that.

## The checks

| Check ID · Severity | Name shown in Data Health | Finds | Suggested action |
|---|---|---|---|
| `config_default_keyed` · Error | Provider account uses a generic ID | A provider config is keyed as `default` even though its label identifies a specific account. | Re-key to the identified account. The preview explains collisions and retained or moved data before confirmation. |
| `legacy_provider_ids` · Error | Usage uses an old provider ID | Events use a provider ID Runway now folds into a canonical provider (for example, `opencode-xai` → `xai`). | Retag to the canonical provider; collisions keep the richer event. |
| `openrouter_model_events` · Error | OpenRouter usage is filed under OpenCode | Events under `opencode`, `opencode-free` or `opencode-byok` whose model id only OpenRouter serves — `<vendor>/<model>:free` or `openrouter/...`. OpenCode tags the *gateway*, not the backend, so older sidecars and BYOK setups filed these under the wrong provider; the BYOK ones sit on `default`, which `lone_default_events` cannot move to another provider. A bare local id such as `qwen/qwen3-coder-30b` is not matched. Fixable only when there is exactly one non-default OpenRouter account. | Preview shows how many events move and how many collide with a copy already under OpenRouter. The fix moves them onto that OpenRouter account and drops the losing copy of each collision (the same tie-break as `legacy_provider_ids`), then rebuilds rollups and windows. Fix each provider group in turn: a message present under both `opencode` and `opencode-byok` is deduplicated when the second group runs. The sidecar now routes new messages the same way. |
| `lone_default_events` · Error | Usage is assigned to a generic account | Usage events or quota history remain under `default` while a specific target identity is known from configured or discovered account data. This can also appear when a default config remains active. | Preview the proposed move and explicitly confirm before moving history to the selected account. |
| `orphan_credential_tags` · Warn | Credential tags point to a missing account | A tag still points at `default` after its provider config was re-keyed or removed. | Delete the tag or repoint it to a configured account. |
| `orphan_credential_sources` · Warn | Credential sources point to a missing account | A stored credential source no longer has anything behind it — its configuration was re-keyed or removed, or the source is filed under two accounts and only one of them still has evidence. | Delete the stranded row; the copy under the real account is kept. |
| `stale_credential_sources` · Warn | Credential sources no machine reports any more | A machine-reported credential row not seen for 14 days, or a metadata-only lookup an older sidecar reported as a credential (an untried “Sidecar credential”). | Forget the rows; a credential still on a machine is reported again on its next collection. |
| `stale_credential_rules` · Warn | Assignment rules no machine matches | An assignment rule you created points at a credential origin no machine has reported for 14 days (or ever). Matching uses the same resolution as attribution, so a rule shadowed everywhere by machine-scoped rules counts as unused. **Never listed:** `provider:<id>` rules (they attribute usage events and are the sidecar's fallback hint), redirect rules, the rules Runway creates itself (identity claim / verification / rotation), `config:` origins, rules younger than 14 days, machine-scoped rules whose machine has not reported in the last day, and everything while no machine is reporting. Running `stale_credential_sources` first removes the evidence rows; both use the same window, so the result is the same. | Delete the rule. If the credential is still unidentified it comes back under “Needs mapping”. The Rules tab marks the same rules (“no credential seen”). For a login that re-keys itself (xAI rotates its refresh token, so its `#<fingerprint>` origin changes), a rule on the old fingerprint is labelled **superseded by a newer login** and the fix moves it onto the current origin instead of deleting it — or deletes it if a rule already covers that origin. |
| `orphan_gauge_series` · Warn | Quota history belongs to an inactive account | A quota card or history series has no config or activity for at least 30 days. | Merge it into a configured account or delete it. Recent activity is checked again before preview and apply. |
| `misidentified_gauge_series` · Error | Gauge data has no supporting evidence | Quota history exists for an account with no usage events, credentials, or config — indicating a transient collector mis-identification (e.g. an email label leaked from another provider's token cache). Unlike `orphan_gauge_series`, there is no 30-day wait — the absence of all five evidence types is the safety gate. A credential the server host found itself (an env var such as `GITHUB_TOKEN`, or Runway's own `github_oauth.json`) counts as evidence too: it is recorded as a `server:` credential source the first time a collection uses it, so an account fed only that way is not flagged — but one that has never completed a collection since upgrading may be, until it does. `server:*:file:*` rows older builds registered for CLI login files on the server host (`oauth_creds.json`, `hosts.yml`, ...) are deleted at startup; the server no longer reads those files, so an account that was fed only that way needs a sidecar (or an env var / Settings credential) and will read as unevidenced until then. | Delete immediately; there is no real usage history to preserve. For a login-keyed provider (GitHub), an *email-shaped* series next to exactly one evidenced login account is the same account re-keyed by its email label: the finding is labelled "email-keyed duplicate of `<login>`" and the default action is **merge** into that login, which keeps the history. |
| `unpriced_models` · Warn | Some token usage has no reliable cost | Token-bearing events have zero stored cost. This includes actionable missing prices and informational configured-zero or source-reported-zero evidence. **Never listed:** free-tier model ids (`:free`, `-free`, `/free`, the bare `free` id, and the `openrouter/free/*` prefix form), and every event on the `opencode-free` provider — a free route is priced at $0 on purpose, so it is not a missing rate and gets no pricing row. | Review per-model evidence; recompute when a rate is available or add a pricing seed. Existing nonzero costs are not lowered. |
| `stale_priced_models` · Warn | Some stored cost estimates use an outdated rate | Messages whose stored `cost_estimated_usd` differs from what today's price table gives at the event's **own date**, grouped per provider and model: a pricing correction made in place (the Sonnet 5.5 cache-read rate, the Haiku 5.5 long-prompt tier) or a new model row that events used to reach through a family fallback (`haiku-5.5` priced as `haiku`). Detection is a cheap SQL aggregate and only a hint; the preview runs the exact per-event computation. **Never listed:** `$0` events (that is `unpriced_models`), events with no stored estimate (legacy rows whose `cost_estimated_usd` was never populated; they are left alone and counted as `skipped_unpopulated` in the preview), free-tier and sentinel model ids, and models with no resolvable price row. | Preview shows counts: the events to refresh, the stored and recomputed estimate totals and how many events keep their `cost_usd` (plus how many would drop to $0). The fix rewrites `cost_estimated_usd` and the cost breakdown, and `cost_usd` only where it currently equals the old estimate; it never touches `cost_reported_usd`, so a reported cost or a `billing_type` change since ingest keeps its history. Rollups are rebuilt for the affected accounts and windows for the affected providers. It is **not** a full re-price (`scripts/recost_events.py` is). Apply is idempotent and the server stays up. |
| `unpriced_claude_accounts` · Warn | Claude accounts without a billing model | A Claude account never had its billing model detected, so the dashboard shows a usage value where a plan shows an estimate. A detected model only ever lands on a **new** account, so every account created before the detection existed is affected. | Preview the accounts and price them from the cards they published. An account whose model you set by hand is never touched. |
| `rollup_drift` · Warn | Cached usage totals do not match events | Lifetime cached totals differ from usage events, including rollup rows left behind after their events were removed. | Rebuild the affected provider/account rollups from current events. |
| `minimax_background_sessions` · Warn | Background work is filed as a conversation | MiniMax Code background-task usage carries the provenance of the session that spawned it, but the rows sit on the background task's own session — so the work shows up in the Sessions view as a conversation of its own. **Never listed:** rows collected before the extractor learned the parent link — they carry no provenance, and only the MiniMax Code runtime store can map them. | Move the rows under the session their provenance names. For the rows without a provenance, run `scripts/link_minimax_background_sessions.py` on the host that ran MiniMax Code. |
| `path_shaped_projects` · Warn | A project label is a path | A usage event carries a directory path as its project label (`D:\Projects\repo`) instead of a name, because the label was derived on a host whose path separator the server did not recognise. One project then lands in a different bucket on every host and every checkout. | Relabel the affected sessions with the name of their project directory. The raw working directory is kept and only the label changes, so no total moves. Rows whose session holds no working directory at all are listed separately and cannot be fixed here. |
| `inconsistent_project_labels` · Warn | A project label does not follow the reported repository | Some usage events are labelled with the name of the folder the work happened in, while newer events from the same directory carry the name of the git repository it belongs to — so one repository is split across two projects. | Move the affected rows onto the repository name the client reported. The working directory and the reported repository are kept, so no total moves. A directory that never reports a repository again keeps its label — the server cannot know which repository it belonged to. |
| `worktree_project_labels` · Warn | A project label is named after a worktree | Some usage events are labelled with the name of a worktree directory (`<repo>/.wt/<slug>`, `.worktrees`, `.worktree`, `.mullion-worktrees`) instead of the repository, so every worktree shows up as a project of its own. New events are labelled correctly; this finds the stored ones. | Relabel the affected sessions with the name of their repository directory (a session that reported a repository keeps that name, so this and `inconsistent_project_labels` never undo each other). The working directory is kept and only the label changes, so no total moves. Back up the database first; the fix asks for a confirmation. |
| `cloud_coarse_events` · Warn | Claude cloud usage from the first collector | Usage events written by the first Claude cloud-runner collector: one summary row per sweep (ids `cloud:<digest>:<n>`), without project or per-model detail. The history-based replacement collector (see the [cloud event contract](claude-cloud-event-contract.md)) does not run while these rows exist. | Preview shows the events, sessions, token sums and how many closed windows would be rebuilt. The fix removes the events, rebuilds the Anthropic rollups from what remains (on every run, so an interrupted fix is finished by applying again) and the closed windows that overlapped the removed events (only in the run that removed them), leaves that collector's two state tables alone, and asks for a confirmation. Once the replacement is enabled it collects the sessions again with project, model and timing detail. |
| `cloud_events_reset` · Warn | Claude cloud usage with outdated labels | Claude cloud-runner usage events stored under another source id than `anthropic-cloud` (older rows carry the generic `server`). Stored events are deduplicated on `(provider_id, event_id)` and never relabelled, so only a re-collection corrects them. | Preview shows the events, sessions, checkpoints and how many closed windows would be rebuilt. The fix removes the history-based cloud events (`cloud:<digest>:m:...` / `:r:...`) **and** the collector's per-session checkpoints, rebuilds the Anthropic rollups (on every run, so an interrupted fix is finished by applying again) and the closed windows that overlapped the removed events (only in the run that removed them), drops the collector's in-memory backlog, and asks for a confirmation. The collector then reads every session's history again with the current labels; applying it twice is a no-op. If a run is interrupted after the delete, applying again repairs the rollups; replaying the window rebuild needs `scripts/reset_cloud_events.py --apply --windows-from ... --windows-to ...` (the first run reports the removed range). |
| `pending_events` · Info | Events need account assignment | Events await evidence-backed or manual account assignment. | Open Fleet and decide the account for each event. |
| `alert_channels` · Warn | Credential alerts have no delivery channel | No active webhook has credential alerts on, so an expired, rejected or failing credential notifies nobody. Not fixable here (it needs your webhook URL); Home shows a banner linking to Settings → Alerts. | Add a Discord or Slack webhook with credential alerts enabled. |

`config_default_keyed` blocks `lone_default_events`, `orphan_credential_tags`,
`orphan_credential_sources` and `openrouter_model_events`; `legacy_provider_ids`
blocks `openrouter_model_events`, `unpriced_models`, `stale_priced_models` and `rollup_drift` — fix the blocking check first if you see a **Blocked**
badge. The expanded check names the prerequisite that needs attention. (The
re-key now carries a provider's credential sources with it, so resolving
`config_default_keyed` first is usually what clears the credential-source
findings too.)

**Pricing rule behind `stale_priced_models`.** A real price change is a **new**
`provider_pricing` row with a new `effective_from`; an in-place edit of an existing
row is only for a seed mistake. Pricing resolves the row in effect at each event's
own date, so a new row never reaches older events, and the check only finds
estimates that disagree with the row that was already in effect at their date.
Editing a rate in place for a real change would rewrite history the next time
the fix runs.

The `config_default_keyed` preview exposes `counts.usage_events_to_move`.
The former `counts.usage_events_retained_on_default` field remains as a
deprecated compatibility key and reports the predicted remaining count (zero)
because the repair now moves that history.

If a scan fails, the page reports the failure and marks any previous report as
stale. Fixes stay disabled until an explicit re-scan succeeds. A job-status
polling failure is shown as unknown and retried; it is not treated as a failed
repair.

## Safety

- Every fix re-validates its own parameters against the database at apply
  time, not just at preview time — a target account has to still exist and
  still not be `default` when the fix actually runs.
- A finding's sample rows only ever show an explicit whitelist of fields
  (provider/account ids, counts, timestamps). A `ProviderConfig` row's
  encrypted credential columns are never included, regardless of which
  check is looking at it.
- Only one scan or fix runs at a time; a fix already in flight makes
  `POST .../apply` return `409` rather than start a second one.
