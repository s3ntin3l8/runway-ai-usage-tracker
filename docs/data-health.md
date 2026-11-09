# Data Health

Settings → Data health runs a fixed set of read-only checks over the
database and, for each one that finds something, offers an in-app fix —
preview first, then apply. The checks live in
`app/services/data_health/checks/` and each one owns its own `detect()` and
its own repair. Most reuse the shared repair helpers in `app/services/maintenance/`; the
rest implement their fix in place. Each check's docstring in that directory is
the detailed spec. See [api-reference.md](api-reference.md) for the routes and
[Offline repair scripts](#offline-repair-scripts) for the command-line
equivalents.

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
4. **A failed fix.** The dialog says what went wrong in plain language —
   never the SQL behind it. When the failure was a busy database, the
   progress the fix had already made is saved and the dialog offers a
   **Retry fix** button that runs the same fix again, against the same group
   and parameters. The preview behind it is refreshed in the same pass, so
   the next preview of that group shows how much is left to do. A fix that
   was *refused* — a confirmation the check asked for and did not get —
   says so in the check's own words.
   A busy database is worth waiting for: the connection waits 30 s for the
   lock before the error surfaces and the fix retries with a growing pause,
   so a stuck apply can hold on for about four minutes before it gives up.

A check with unresolved findings can block another check from being
applied until it's fixed — the dependency order below reflects that.

## The checks

| Check · Severity | Name shown | Finds | Fix |
|---|---|---|---|
| `config_default_keyed` · Error | Provider account uses a generic ID | A provider config keyed `default` although its label names an account. | Re-key to the identified account; the preview explains collisions. |
| `legacy_provider_ids` · Error | Usage uses an old provider ID | Events under a provider id Runway now folds into a canonical one (`opencode-xai` → `xai`). | Retag; collisions keep the richer event. |
| `openrouter_model_events` · Error | OpenRouter usage is filed under OpenCode | OpenCode-tagged events whose model id only OpenRouter serves. | Move onto the single non-default OpenRouter account. |
| `lone_default_events` · Error | Usage is assigned to a generic account | Events or quota history left on `default` while a specific account is known. | Move to the selected account after confirmation. |
| `orphan_credential_tags` · Warn | Credential tags point to a missing account | A tag still points at `default` after its config was re-keyed or removed. | Delete the tag or repoint it. |
| `orphan_credential_sources` · Warn | Credential sources point to a missing account | A credential source with nothing behind it. | Delete the stranded row; the real account's copy stays. |
| `stale_credential_sources` · Warn | Credential sources no machine reports any more | A machine-reported source not seen for 14 days. | Forget the rows; a live credential is re-reported. |
| `stale_credential_rules` · Warn | Assignment rules no machine matches | A user-created assignment rule for an origin no machine reported for 14 days. | Delete the unused rules. |
| `orphan_gauge_series` · Warn | Quota history belongs to an inactive account | A quota series with no config or activity for 30 days. | Merge into a configured account or delete. |
| `misidentified_gauge_series` · Error | Gauge data has no supporting evidence | Quota history for an account with no events, credentials or config. | Merge or delete the series. |
| `unpriced_models` · Warn | Some token usage has no reliable cost | Token-bearing events with zero stored cost (free-tier ids are never listed). | Recompute once a rate exists, or add a pricing seed. |
| `stale_priced_models` · Warn | Some stored cost estimates use an outdated rate | Estimates that differ from the rate in effect at the event's date. | Recompute the estimates. |
| `fallback_priced_events` · Warn | Old usage was billed through a family price | Events priced through a family fallback row. | Re-price against the exact row. |
| `unpriced_claude_accounts` · Warn | Claude accounts without a billing model | Claude accounts created before billing detection. | Set the billing type from the account's published cards. |
| `rollup_drift` · Warn | Cached usage totals do not match events | `usage_period_rollup` disagrees with `usage_events`. | Rebuild the rollups for the pair. |
| `minimax_background_sessions` · Warn | Background work is filed as a conversation | MiniMax background-task rows filed as their own sessions. | File them under the spawning session. |
| `path_shaped_projects` · Warn | A project label is a path | A project label that is a directory path. | Relabel with the directory name. |
| `inconsistent_project_labels` · Warn | A project label does not follow the reported repository | One repository split across folder-named and repo-named projects. | Move rows onto the reported repository name. |
| `worktree_project_labels` · Warn | A project label is named after a worktree | Labels named after a worktree directory. | Relabel with the repository name. |
| `cloud_coarse_events` · Warn | Claude cloud usage from the first collector | Coarse summary rows from the first cloud collector. | Remove them and rebuild rollups; the replacement collector re-reads history. |
| `cloud_events_reset` · Warn | Claude cloud usage with outdated labels | Cloud events stored under an outdated source id. | Remove events and checkpoints; the collector re-reads with current labels. |
| `cloud_session_lead_repo` · Warn | A cloud session's events carry no project label | A multi-repository cloud session with unlabelled events. | Label them with the repository its labelled events name most often. |
| `pending_events` · Info | Events need account assignment | Events awaiting account assignment. | Decide the account in Fleet. |
| `alert_channels` · Warn | Credential alerts have no delivery channel | No active webhook has credential alerts on. | Not fixable here; add a webhook under Settings → Alerts. |

Dependencies are declared per check (`blocked_by`): `config_default_keyed` blocks
`lone_default_events`, `orphan_credential_tags`, `orphan_credential_sources` and
`openrouter_model_events`; `legacy_provider_ids` blocks `openrouter_model_events`,
`unpriced_models`, `stale_priced_models`, `fallback_priced_events` and
`rollup_drift`; `cloud_events_reset` blocks `cloud_session_lead_repo`. A
**Blocked** badge names the prerequisite.

**Pricing rule behind `stale_priced_models`.** A real price change is a new
`provider_pricing` row with a new `effective_from`; editing a row in place is only
for a seed mistake. Pricing resolves the row in effect at each event's date, so a
new row never reaches older events.

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

## Offline repair scripts

The `scripts/` wrappers run the same repairs against the database directly,
without starting the server. **Stop the server first**: they write to SQLite
without coordination. The logic lives in `app/services/maintenance/`, which the
in-app fixes call too, so both routes give the same result. Most scripts preview
by default and write only with `--apply`; some apply unless given `--dry-run`.
Check `--help` before running one. `--provider` scopes a repair to one provider id.

| Script | Purpose |
|---|---|
| `backfill_rollups.py` | Rebuild `usage_period_rollup` from `usage_events`. |
| `backfill_cache_costs.py` | Backfill per-component cost columns, then rebuild rollups. |
| `backfill_project_context.py` | Backfill working-directory and project context onto events. |
| `backfill_claude_billing_type.py` | Price Claude accounts created before billing detection. |
| `backfill_claude_dimensions.py` | Backfill Claude Code per-message dimensions. |
| `backfill_opencode_effort.py` | Backfill the OpenCode `variant` into `usage_events.effort`. |
| `consolidate_session_projects.py` | Relabel each session's events to its root project (the `worktree_project_labels` check). |
| `label_cloud_session_lead_repo.py` | The `cloud_session_lead_repo` repair. |
| `link_minimax_background_sessions.py` | The `minimax_background_sessions` repair; run it on the host that ran MiniMax Code. |
| `remove_cloud_coarse_events.py` | The `cloud_coarse_events` repair. |
| `reset_cloud_events.py` | The `cloud_events_reset` repair; `--windows-from` / `--windows-to` replay the window rebuild of an interrupted run. |
| `reprice_fallback_priced_events.py` | The `fallback_priced_events` repair. |
| `recost_events.py` | Full re-price of `cost_usd` and the derived cost tables. `--estimate-only` applies the `stale_priced_models` rule; `--skip-rollups` and `--skip-windows` leave derived tables alone. Only needed after a calculator change; for a pricing correction use the in-app fix. |
| `assign_default_events.py` | Assign events on `default` to their identified account. |
| `collapse_default_account_events.py` | Collapse duplicate events left by a stale `account_id`. |
| `dedup_usage_windows.py` | Collapse duplicate `usage_windows` rows from a jittering provider reset time. |
| `normalize_sidecar_ids.py` | Merge sidecar ids that drifted between FQDN and short host names. |

**Legacy one-shots.** These fixed specific past data problems and are only
needed on a database that still has them: `cleanup_anthropic_sonnet_weekly.py`,
`cleanup_antigravity.py`, `cleanup_chatgpt_stale_window.py`,
`cleanup_minimax_monthly_cards.py`, `cleanup_opencode_free_card.py`,
`cleanup_opencode_kimi_card.py` (stale gauge cards);
`merge_gemini_default_account.py`, `merge_antigravity_accounts.py` (duplicate
account identities); `reclassify_opencode_providers.py`,
`reclassify_antigravity_models.py`, `reclassify_chatgpt_models.py`,
`reclassify_hermes_pending.py` (re-tag events); `backfill_argon_models.py`;
`migrate_kimi_coding_key.py` (moves a `kimi_coding` key out of the
`session_cookie` slot).
