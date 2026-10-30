# Maintenance and repair scripts

The in-app repair tooling is described in [`data-health.md`](data-health.md).
This document covers the **command-line** equivalents: the `scripts/` wrappers
that run the same repairs against a database directly, without starting the
server.

> **Stop the server first.** These scripts write to the SQLite database
> directly. Running them against a database the server has open is not
> supported.

## The plan/apply model

Almost every script follows the same two-phase contract:

| Phase | Flag | Effect |
|---|---|---|
| Preview | *(default)* or `--dry-run` | Report what would change. **No writes.** |
| Apply | `--apply` | Perform the change. |

A few scripts use `--provider` to scope the repair to one provider id:

```bash
python3 scripts/recost_events.py --provider antigravity
python3 scripts/reclassify_opencode_providers.py --providers opencode-ollama --dry-run
```

`recost_events.py` also accepts `--skip-rollups` and `--skip-windows` to leave
the derived tables alone, and `--estimate-only` to refresh only stale computed
estimates (events with no stored estimate are skipped and reported as such, and
reported costs and the shown total of events that do not display the estimate
are left alone). That is the rule the Data Health `stale_priced_models`
fix applies, and the normal path after a pricing correction. A plain run is the
full re-price and also rewrites `cost_usd` where the estimate/reported selection
changed (for example after a `billing_type` change).

## Backfills

Reconstruct data that was not recorded when the event was first ingested.

| Script | What it rebuilds |
|---|---|
| `backfill_rollups.py` | Rebuild `usage_period_rollup` from existing `usage_events`. |
| `backfill_cache_costs.py` | Backfill the per-component cost columns on `usage_events`, then rebuild the rollups. |
| `backfill_project_context.py` | Backfill the working-directory / project context onto existing events. |
| `consolidate_session_projects.py` | Relabel each session's events to its root project (the repository name the session reported, else its root directory, cut at `.claude`/`.wt`/`.worktrees`/`.worktree`/`.mullion-worktrees`). Applies by default; `--dry-run` only reports. The in-app equivalent is the `worktree_project_labels` Data Health check. |
| `backfill_claude_billing_type.py` | Price the Claude accounts that were created **before** billing detection existed. |
| `backfill_claude_dimensions.py` | Backfill the Claude Code per-message dimensions onto already-ingested events. |
| `backfill_argon_models.py` | Repair already-ingested Gemini 4 Argon rows — rewrite the stored `model_id` to `gemini-4-argon` and re-price the event in the same pass. |
| `backfill_opencode_effort.py` | Backfill the OpenCode message `variant` onto already-ingested `usage_events.effort`. |

## Cleanups

Remove rows that should not exist any more.

| Script | What it removes |
|---|---|
| `cleanup_antigravity.py` | One-shot cleanup of stale Antigravity data. |
| `cleanup_anthropic_sonnet_weekly.py` | The stale Anthropic "Sonnet Weekly" gauge card. |
| `cleanup_chatgpt_stale_window.py` | The stale ChatGPT/Codex gauge cards left behind by a superseded window. |
| `cleanup_minimax_monthly_cards.py` | The stale MiniMax "monthly" gauge cards. |
| `cleanup_opencode_free_card.py` | The stale OpenCode "rolling free" gauge card. |
| `cleanup_opencode_kimi_card.py` | The stale OpenCode-derived Kimi Coding gauge card. |
| `remove_cloud_coarse_events.py` | The first Claude cloud-runner collector's coarse usage events (report only unless `--apply`), then rebuilds the Anthropic rollups and the closed windows that overlapped them; the `cloud_coarse_events` Data Health check runs the same repair. |
| `reset_cloud_events.py` | The Claude cloud-runner events (`cloud:<digest>:m:...` / `:r:...`) and the collector's per-session checkpoints (report only unless `--apply`), then rebuilds the Anthropic rollups and the closed windows that overlapped them so the collector re-reads the history with the current labels; the `cloud_events_reset` Data Health check runs the same repair. `--windows-from` / `--windows-to` replay the window rebuild of an interrupted run. |

## Merges and collapses

Resolve a duplicated or generic account identity.

| Script | What it collapses |
|---|---|
| `collapse_default_account_events.py` | Duplicated `usage_events` rows left over from a stale `account_id`. |
| `merge_gemini_default_account.py` | A duplicated Gemini account identity (`default` → email). **Standalone** — it does not reuse the shared maintenance helpers. |
| `merge_antigravity_accounts.py` | Merge Antigravity account identities. |
| `assign_default_events.py` | Assign events sitting under a generic `default` account to their identified account. **Standalone.** |

## Reclassification

Re-tag events and rows under the right provider or model.

| Script | What it re-tags |
|---|---|
| `reclassify_opencode_providers.py` | One-shot reclassification of OpenCode events mislabelled before issue #182's fix. |
| `reclassify_antigravity_models.py` | Antigravity models stored under a pre-Argon id. |
| `reclassify_chatgpt_models.py` | Repair ChatGPT `usage_events` written before full Codex model-slug preservation. |
| `reclassify_hermes_pending.py` | Reclassify Hermes pending usage events (and legacy `usage_events`) to canonical providers. |

## Repairs

| Script | What it repairs |
|---|---|
| `recost_events.py` | The **full** re-price: recompute `cost_usd` on `usage_events` and rebuild the derived cost tables. Only needed after a calculator change or a change of how the displayed cost is chosen; for a pricing correction use the Data Health `stale_priced_models` fix (server stays up) or `--estimate-only`, which applies the same rule. |
| `link_minimax_background_sessions.py` | File the MiniMax Code background-task rows under the session that spawned them. Run it on the host that ran MiniMax Code — only that host has the runtime store. |
| `migrate_kimi_coding_key.py` | One-shot migration: the `kimi_coding` API key stored in the `session_cookie` slot. |

## Where the logic lives

The scripts are thin wrappers. The repair logic is in
`app/services/maintenance/`, and each module there exposes a `plan_*` (preview)
and an `apply_*` (write) pair. Some modules use the shared chunked-SQL helpers
in `app/services/maintenance/_chunked_sql.py` so a large repair does not hold
one transaction open for the whole table; others implement their own plan and
apply.

`app/services/data_health/checks/` calls into the same helpers, which is why an
in-app fix and the equivalent script produce the same result.

> Not every maintenance module has a plan/apply pair. `event_cost`,
> `legacy_providers`, `rollups` and `windows` implement only the operation
> itself, with the preview living in the calling script.

## Related

- [`data-health.md`](data-health.md) — the in-app version of these repairs.
- [`architecture.md`](architecture.md) — the data model the repairs operate on.
- [`api-reference.md`](api-reference.md) — the data-health endpoints.
