# UI/UX audit — Runway webapp

**Scope.** The React SPA in `webapp/`: Home, provider detail, Insights, History, Fleet
and Settings, at desktop (1440px) and phone (390px) viewports, in both themes. The native
sidecar tray app and the installer are out of scope.

**Status.** The P0 and P1 findings are fixed in the same branch that added this document
(`fix/webapp): resolve P0/P1 findings from the UI/UX audit`). Everything below P1 is
tracked as a GitHub issue (see [Deferred backlog](#deferred-backlog)).

---

## How the audit was run

| Layer | Method |
|---|---|
| Product IA and flows | Every page and component under `webapp/src/features/**` plus the routing in `app.tsx` |
| Visual reality | The committed renders in `assets/screenshots/` — the real UI, not a mockup |
| Interaction and accessibility | `webapp/src/components/ui/**`, `layout/**`, `charts/**`, plus WCAG 2.x contrast math over the real token values |
| Mobile ergonomics | Breakpoint, safe-area, overflow and touch-target analysis of `AppShell`, `BottomNav`, `PageHeader` and `useMediaQuery` |
| Independent slices | Four parallel read-only audits (design system, responsive, IA/journeys, tables/charts/formatting), each finding cross-checked against the source before it was accepted |

Every P0/P1 below was re-verified against the source before it was written down; the
parallel audits were inputs, not verdicts.

### What the code already does well

These were checked and left alone — a fix here would have been a regression:

- A real semantic token system with a dark/light pair; components hold no raw hex.
- `prefers-reduced-motion` honoured globally (`webapp/src/styles/index.css`).
- Correct Radix roles for tabs, select, switch and dialog; `aria-valuetext` on the
  quota gauge.
- One `NAV_ITEMS` source shared by the desktop sidebar and the mobile bottom nav.
- URL-synced `range` / `tab` / `account` / `sidecar` state, so a link reproduces the view.
- Optimistic layout reorder with rollback.
- Confirm dialogs that name their target on the credential flows.
- Sortable table headers as real `<button>` with `aria-sort`.
- `formatCost` distinguishing `$0.00` / `< $0.01` / `—`.

---

## Severity model

- **P0** — a supported configuration renders the surface broken.
- **P1** — a serious defect with no workaround for the affected user.
- **P2 / P3** — polish; deferred to the backlog.

---

## Fixed in this branch

### P0

| # | Finding | Where | Fix |
|---|---|---|---|
| P0-1 | The page header collapsed on notched iPhones in standalone PWA mode. `h-14` is a fixed 56px **border-box** height, so the ~47px safe-area inset was subtracted *from* the band instead of added to it: the title and the header actions overflowed the band and collided with the page. The provider page escaped it only by accident. | `components/layout/PageHeader.tsx` | `min-h-14`, so the band grows to fit the inset plus its content |
| P0-2 | An unknown `?tab=` rendered a blank provider screen: Radix renders a panel only for a matching value, so the header and an empty tab strip showed with no content and no way back. | `features/provider/ProviderPage.tsx` | The tab value is validated against the seven known tabs and falls back to `overview` |

### P1 — navigation and information architecture

| # | Finding | Where | Fix |
|---|---|---|---|
| P1-1 | The provider detail page was an orphan route: no nav item was active on the second-most-visited screen, in either nav. | `components/layout/nav.ts`, `Sidebar.tsx`, `BottomNav.tsx` | One `isNavItemActive` matcher drives the class and `aria-current`; Home stays active under `/provider/*` |
| P1-2 | "Back" used `navigate(-1)`. On a bookmark, a deep link or a refresh there is no in-app history entry, so it left the dashboard entirely. | `features/provider/ProviderPage.tsx` | Back goes Home |
| P1-3 | Home's zero state was a dead end *and* misfired: it said "configure a provider" even after the user had just configured one, because the list it reads comes from the fleet only. | `features/home/HomePage.tsx` | Three distinct states — nothing set up ("Add a provider" / "Connect a sidecar"), a machine already paired but no provider yet ("Add a provider" / "Manage sidecars"), and set up but nothing reported ("Collect now") |
| P1-4 | The Debug tab sat in the primary tab strip for every user, and its raw capture is admin-gated server-side, so a normal user got "Capture failed". | `features/provider/ProviderPage.tsx` | Debug moves into the "More provider actions" menu and only joins the tab strip once it is open |

### P1 — accessibility and keyboard operability

| # | Finding | Where | Fix |
|---|---|---|---|
| P1-5 | No skip link, and `<main>` had no id: every page load required tabbing through the whole nav first (WCAG 2.4.1). | `components/layout/AppShell.tsx` | Skip link to `#main`, with a focusable `id="main"` |
| P1-6 | Clickable table rows were unreachable by keyboard — no `tabIndex`, no role, no key handler, and `aria-expanded` on a non-focusable `<tr>`. The whole drill-down layer (session detail, cost split, window detail) was keyboard-inaccessible. | `features/provider/SessionsTable.tsx`, `CostTab.tsx`, `features/history/HistoryPage.tsx` | The chevron cell holds a real `<button aria-expanded>`; the row stays clickable for the mouse |
| P1-7 | Charts had no accessible text alternative — no `aria`, `role` or `tabIndex` anywhere in `components/charts/**`, and the ECharts aria component was not registered. The primary information on History, Activity and the heatmap existed only as pixels. | `components/charts/**` | `ariaLabel` on the chart wrapper (`role="img"`), a visually-hidden data list for every chart whose information is only pixels, and legends for the stacked bars |
| P1-8 | Keyboard reordering of the provider grid was dead: the card's own `onKeyDown` came after `{...listeners}` in the prop order, so it *replaced* dnd-kit's keyboard activator and the `KeyboardSensor` never received a keydown. The card also ignored Space, unlike its siblings. | `features/home/ProviderGrid.tsx` | The sensor's handler is composed rather than replaced; Space starts the keyboard drag, Enter opens the provider |
| P1-9 | Tooltips over role-less markup were hover-only: Radix opens on focus, but a `<span>` badge or a `<p>` has no tab stop, so the hints ("Not used" reason, offline reason, collection path) were unreachable. | `components/ui/Tooltip.tsx` | The trigger is wrapped in a focusable host |
| P1-10 | Token segment colour was index-based, so a token type changed colour when "Exclude cache" was on (Reasoning moved from `--chart-5` to `--chart-3`). | `components/charts/TokenBar.tsx`, `TokenDonut.tsx` | Colour by segment key, not by position; bar and donut agree |
| P1-11 | Stacked bar charts had no legend, so on touch "is the green third block Output or Cache read?" was unanswerable. | `components/charts/RankBar.tsx`, `TopModelsBar.tsx`, `TrajectoryChart.tsx` | Legends on every stacked chart |

### P1 — contrast and colour

| # | Finding | Measured | Fix |
|---|---|---|---|
| P1-12 | `--fg-subtle` failed 4.5:1 in both themes and carried the table headers and the 11px KPI labels. | 3.9:1 on a card (dark), 3.1:1 on the canvas (light) | dark `#858591` (5.2:1), light `#6e6e77` (4.8:1) |
| P1-13 | Accent-as-text and the light-theme badge/banner text failed contrast. | accent 4.0:1 (dark), light badge text 3.1–4.4:1, banner text 3.0:1 | A dedicated `--accent-text` token plus darker light-theme status tokens |
| P1-14 | The danger button's white label failed contrast in dark. | 3.5:1 | A `--critical-strong` fill (4.7:1) |
| P1-15 | The light theme kept the dark categorical chart palette, below the 3:1 floor for graphical objects. | 2.1–2.6:1 on white | A light-mode `--chart-*` block, all at or above 3.5:1 |

### P1 — mobile ergonomics

| # | Finding | Where | Fix |
|---|---|---|---|
| P1-16 | The bottom nav is `h-16` **plus** the home-indicator inset (≈98px), but the page reserved a fixed `pb-20` (80px) — the last ~18px of every page sat under the nav. | `components/layout/AppShell.tsx` | `pb-[calc(5rem+env(safe-area-inset-bottom))]` |
| P1-17 | The mobile sheet had no close button — the desktop dialog had one, the sheet only had a decorative handle — so touch users had to guess a swipe-down. | `components/ui/ResponsiveDialog.tsx` | A close control in the sheet, with a `dismissible` prop for dialogs that must stay open |
| P1-18 | The primary mobile actions were 32–36px tall. | `components/ui/Button.tsx`, `TimeRangePicker.tsx`, `Tabs.tsx` | A 44px floor below the `lg` breakpoint; desktop keeps its compact rhythm |

### P1 — found by the strengthened accessibility guard

The guard described under [Guardrails added](#guardrails-added) was rewritten during
review: it now asserts on rule ids instead of an impact tier, renders every surface
with populated data, and waits for the queries to settle before scanning. Scanning the
trees users actually meet surfaced four defects the first version of the guard could
not see.

| # | Finding | Where | Fix |
|---|---|---|---|
| P1-25 | Six metric switchers were Radix tab lists with **no tab panel**: every trigger's `aria-controls` pointed at an element that did not exist, and a screen reader announced a "tab" with nothing to jump to. They switch what one panel shows; they are not tabs. | `features/history/HistoryPage.tsx`, `features/insights/{Overall,TopHosts,TopModels,TopProjects,TopSessions}Card.tsx` | A `SegmentedControl` built on native radio inputs: a named group, the chosen option announced as selected, arrow-key movement and a single tab stop — without the tab role |
| P1-26 | Every select trigger was an unnamed combo box. A combobox takes its name from the author, not from its content, so the selected value inside was never announced as the control's name. | `components/ui/Select.tsx` and its 19 call sites | `SelectTrigger` now requires a `label`, so the name is enforced by the compiler rather than by convention |
| P1-27 | The quota gauges had no accessible name: a screen reader announced "42%" with no indication of what was at 42%. | `components/ui/Gauge.tsx` and its call sites | The meter names what it measures ("Claude quota used", "Month budget used") |
| P1-28 | The chevron column of the session and cost tables had an unnamed `<th>`, announced as an empty column. | `features/provider/SessionsTable.tsx`, `CostTab.tsx` | The spacer header is named "Details" |

### P1 — flow, states and honesty

| # | Finding | Where | Fix |
|---|---|---|---|
| P1-19 | Insights and History rendered "no data" when the *request failed* — no `isError` branch anywhere on either page, and no retry. | `features/insights/**`, `features/history/HistoryPage.tsx` | Error states with a Retry action on the Insights cards and on History's chart and quota-window archive. History's four stat tiles (`deltas`, `anomalies`) still show their last-known or zero value when their request fails — they have a `loading` state but no `isError` branch; that remainder is filed under [#948](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/948) |
| P1-20 | Rankings were truncated without saying so: Insights capped at 12, History at 50, Activity's top sessions at 10, while the titles read "Top hosts", implying a complete ranking. | `features/insights/queries.ts` and the cards | The titles state the cap ("Top 12 hosts"). A real "12 of N" needs a backend total — see the backlog |
| P1-21 | "At risk" contradicted the grid's own colours: the rail ranked by gauge **and** forecast while the grid coloured by gauge only (90% / 70% thresholds), so a 61% provider sat under "At risk" with a green bar — and the same providers then appeared again in the grid below. | `features/home/risk.ts`, `AtRiskRail.tsx` | The section is "Needs attention", with a subtitle that says why the cards are there; the bar keeps meaning "used now" and the badge keeps meaning "time to limit" |
| P1-22 | Archiving or resetting a provider fired straight from a dropdown item, with no confirmation, and the page then navigated away. | `features/provider/ProviderPage.tsx` | A confirm that names the provider, an Undo in the toast, and no navigation away |
| P1-23 | The same money was labelled seven different ways, none of them "cost": "Usage value", "Projected usage value", "Daily usage value", "Estimated usage value", "Daily burn", "Projected EOM", "Cost". | `features/home/AggregateStrip.tsx`, `features/provider/ProviderKpis.tsx`, `CostTab.tsx`, `lib/billingType.ts` | One vocabulary: "Cost" for what the provider reported, "Estimated cost" for what we compute, and projections say so. The rule lives in `lib/billingType.ts` and both the KPI strip and the Cost tab read it, so the same number cannot appear under two names on one page |
| P1-24 | "Global insights · All time" sat under the page's range picker, which does not apply to it — a silent scope mismatch behind a 13px heading. | `features/insights/InsightsPage.tsx` | The lifetime block is visually separated and says "All time — not affected by the range filter"; the section below states its range |

---

## Guardrails added

Both run inside the required frontend CI job, so the fixes cannot silently regress:

- **`webapp/src/test/a11y.test.tsx`** — axe-core over ten surfaces: the app shell, Home,
  the provider detail (overview, sessions and cost tabs), Fleet, Settings, History,
  Insights, and the destructive-action confirm dialog.

  Two things make it a guard rather than a smoke test:

  - **It asserts on rule ids, not on an impact tier.** In axe-core 4.14 `heading-order`,
    `region`, `landmark-one-main` and `landmark-unique` are all `moderate`, so a
    "serious or critical" filter never failed on the landmark and heading half of the
    guarantee. Every violation now fails the build unless its rule id is in an explicit
    exception list, and each exception carries its reason.
  - **It scans the tree a user meets.** The surfaces render with populated API mocks,
    and the scan waits for the queries to settle. With empty mocks the provider page
    only ever rendered its "No data for this provider" empty state, so the tabs, the
    sessions table, the cost split and the dialogs were never scanned at all.

  Colour contrast is excluded on purpose: jsdom has no CSS engine, so axe cannot judge
  it — see the token-level check below.
- **`webapp/src/styles/contrast.test.ts`** — computes the WCAG ratios from the token
  values themselves, in both themes, for every pair the UI actually renders (body,
  secondary and 11px label text, badge text on its tint, the primary and danger button
  labels, and the six chart series). This is what covers the theme axis that axe cannot.

Plus behavioural tests for the fixes that are not visible in a screenshot: the skip
link, the keyboard drag, the `?tab=` fallback, the zero state, the error states and the
destructive-action confirm.

---

## Evidence

`make screenshots-audit` captures every page at 390×844 (phone) and 768×1024 (tablet),
dark and light, into `docs/ui-ux-audit/evidence/`. They are viewport-sized rather than
full-page on purpose: a fixed bottom nav is captured at its viewport position, so a
full-page shot would not show whether the last row of content clears it.

The README profile (`make screenshots`) is desktop-only, so before this the mobile
layouts had no visual record at all.

---

## Deferred backlog

Tracked as GitHub issues rather than footnotes:

- [#948 Data-honesty polish](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/948) — formatting
  helpers, raw number formatting, timezone leakage, errors masked as empty states
- [#949 Backend `total` for the rankings](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/949) —
  so the titles can read "Top 12 **of N**"
- [#950 Mobile and table density](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/950) — overflowing
  columns, heatmap cells, the settings redirect
- [#951 Chart colour and legend semantics](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/951) — the
  six-colour ceiling, axis truncation, a duplicated rank bar
- [#952 Terminology and small accessibility gaps](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/952) —
  deep links, the duplicate provider status, empty column headers, focus rings
- [#953 Playwright UX smoke suite and its CI job](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/953) —
  what axe and the contrast guard cannot catch

The findings themselves:

- **Data-honesty polish** — `formatTokens` rounding 1.4K to "1K" while its sibling
  `formatNumber` keeps a decimal; `formatPct` defaulting to zero digits so 99.6% renders
  "100%"; `HistoryChart` bypassing `formatCost`; a backend `Total:` that can contradict a
  chart's own segments; raw `toLocaleString()` in eight or more places; browser-timezone
  leakage in three dialogs.
