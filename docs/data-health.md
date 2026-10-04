# Data Health

Settings → Data health runs a fixed set of read-only checks over the
database and, for each one that finds something, offers an in-app fix —
preview first, then apply. The repair logic lives in
`app/services/maintenance/`; the checks in `app/services/data_health/checks/`
just detect *when* to offer it. See [api-reference.md](api-reference.md) for
the underlying routes.

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
| `lone_default_events` · Error | Usage is assigned to a generic account | Usage events or quota history remain under `default` while a specific target identity is known from configured or discovered account data. This can also appear when a default config remains active. | Preview the proposed move and explicitly confirm before moving history to the selected account. |
| `orphan_credential_tags` · Warning | Credential tags point to a missing account | A tag still points at `default` after its provider config was re-keyed or removed. | Delete the tag or repoint it to a configured account. |
| `orphan_credential_sources` · Warning | Credential sources point to a missing account | A stored credential source no longer has anything behind it — its configuration was re-keyed or removed, or the source is filed under two accounts and only one of them still has evidence. | Delete the stranded row; the copy under the real account is kept. |
| `stale_credential_sources` · Warning | Credential sources no machine reports any more | A machine-reported credential row not seen for 14 days, or a metadata-only lookup an older sidecar reported as a credential (an untried “Sidecar credential”). | Forget the rows; a credential still on a machine is reported again on its next collection. |
| `stale_credential_rules` · Warning | Assignment rules no machine matches | An assignment rule you created points at a credential origin no machine has reported for 14 days (or ever). Matching uses the same resolution as attribution, so a rule shadowed everywhere by machine-scoped rules counts as unused. **Never listed:** `provider:<id>` rules (they attribute usage events and are the sidecar's fallback hint), redirect rules, the rules Runway creates itself (identity claim / verification / rotation), `config:` origins, rules younger than 14 days, machine-scoped rules whose machine has not reported in the last day, and everything while no machine is reporting. Running `stale_credential_sources` first removes the evidence rows; both use the same window, so the result is the same. | Delete the rule. If the credential is still unidentified it comes back under “Needs mapping”. The Rules tab marks the same rules (“no credential seen”). |
| `orphan_gauge_series` · Warning | Quota history belongs to an inactive account | A quota card or history series has no config or activity for at least 30 days. | Merge it into a configured account or delete it. Recent activity is checked again before preview and apply. |
| `misidentified_gauge_series` · Error | Gauge data has no supporting evidence | Quota history exists for an account with no usage events, credentials, or config — indicating a transient collector mis-identification (e.g. an email label leaked from another provider's token cache). Unlike `orphan_gauge_series`, there is no 30-day wait — the absence of all five evidence types is the safety gate. A credential the server host found itself (an env var such as `GITHUB_TOKEN`, or a local file) counts as evidence too: it is recorded as a `server:` credential source the first time a collection uses it, so an account fed only that way is not flagged — but one that has never completed a collection since upgrading may be, until it does. | Delete immediately; there is no real usage history to preserve. For a login-keyed provider (GitHub), an *email-shaped* series next to exactly one evidenced login account is the same account re-keyed by its email label: the finding is labelled "email-keyed duplicate of `<login>`" and the default action is **merge** into that login, which keeps the history. |
| `unpriced_models` · Warning / Info | Some token usage has no reliable cost | Token-bearing events have zero stored cost. This includes actionable missing prices and informational configured-zero or source-reported-zero evidence. | Review per-model evidence; recompute when a rate is available or add a pricing seed. Existing nonzero costs are not lowered. |
| `rollup_drift` · Warning | Cached usage totals do not match events | Lifetime cached totals differ from usage events, including rollup rows left behind after their events were removed. | Rebuild the affected provider/account rollups from current events. |
| `pending_events` · Info | Events need account assignment | Events await evidence-backed or manual account assignment. | Open Fleet and decide the account for each event. |
| `alert_channels` · Warn | Credential alerts have no delivery channel | No active webhook has credential alerts on, so an expired, rejected or failing credential notifies nobody. Not fixable here (it needs your webhook URL); Home shows a banner linking to Settings → Alerts. | Add a Discord or Slack webhook with credential alerts enabled. |

`config_default_keyed` blocks `lone_default_events`, `orphan_credential_tags`
and `orphan_credential_sources`; `legacy_provider_ids` blocks `unpriced_models`
and `rollup_drift` — fix the blocking check first if you see a **Blocked**
badge. The expanded check names the prerequisite that needs attention. (The
re-key now carries a provider's credential sources with it, so resolving
`config_default_keyed` first is usually what clears the credential-source
findings too.)

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
