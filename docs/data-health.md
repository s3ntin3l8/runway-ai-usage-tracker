# Data Health

Settings → Data health runs a fixed set of read-only checks over the
database and, for each one that finds something, offers an in-app fix —
preview first, then apply. The repair logic lives in
`app/services/maintenance/`; the checks in `app/services/data_health/checks/`
just detect *when* to offer it. See [api-reference.md](api-reference.md) for
the underlying routes.

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

| Check | Severity | Finds | Fix |
|---|---|---|---|
| `config_default_keyed` | Error | A provider's config is still keyed `account_id="default"` even though its label already carries a real identity | Re-key the config (and its credential tags, webhooks, and gauge series) onto the real account id |
| `legacy_provider_ids` | Error | Events under an OpenCode-sibling provider id Runway now folds into a canonical provider (e.g. `opencode-xai` → `xai`) | Retag onto the canonical provider, resolving any collision by keeping the richer event |
| `lone_default_events` | Error | Events sitting alone under a stale `default` account for a provider that has since been configured under a real one | Reassign to the provider's other configured account — not fixable in-app if there's no unambiguous target (surfaces the reason why) |
| `orphan_credential_tags` | Warning | A credential tag still points at `default` after that provider's `default` config was rekeyed or removed | Delete the tag, or repoint it onto a configured account |
| `orphan_gauge_series` | Warning | A dashboard card / quota-history series for an account with no config and no recent events | Merge into a configured account, or delete the stale series |
| `unpriced_models` | Warning | Token-bearing events priced at $0.00 | Recompute cost for the provider (never lowers an already-nonzero cost); reports whether a seed pricing row is what's actually missing |
| `rollup_drift` | Warning | `usage_period_rollup`'s cached lifetime totals no longer match a straight aggregate of `usage_events` | Rebuild rollups for the affected `(provider, account)` pair from events |
| `pending_events` | Info | Events awaiting evidence-backed or manual account assignment | Not fixed here — links to the Fleet page, where each event gets a per-event decision |

`config_default_keyed` blocks `lone_default_events` and `orphan_credential_tags`;
`legacy_provider_ids` blocks `unpriced_models` and `rollup_drift` — fix the
blocking check first if you see a **Blocked** badge.

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
