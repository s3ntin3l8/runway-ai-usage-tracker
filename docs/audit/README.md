# Audit reports

Every audit in this repo — a UI/UX audit, a performance investigation, a
security review — is one markdown file in this folder. The convention is the
same in every project: **the report is committed, the evidence stays local.**

## Layout

```
docs/audit/
  README.md                          <- this file: the convention and the index
  YYYY-MM-<scope>-<topic>.md         <- one report per audit
  assets/                            <- gitignored; the captures, local only
    YYYY-MM-<scope>-<topic>/
```

`docs/audit/assets/` is gitignored. The captures are regenerable from a demo
instance, they are not part of the product, and a folder of PNGs per audit
adds permanent history weight for something nobody reads after the findings
are fixed. The **report** is the durable artifact: what was found, why it
mattered, what was changed, and what was deliberately left for later.

A report therefore never links to a file that is not in the repository. It
describes how to regenerate the captures and links to the command, not to
the images.

## Naming

`YYYY-MM-<scope>-<topic>.md`

| Part | Meaning | Examples |
|---|---|---|
| `YYYY-MM` | when the audit ran | `2026-10` |
| `<scope>` | what was audited | `webapp`, `sidecar`, `server` |
| `<topic>` | what kind of audit it was | `ui-ux`, `performance`, `security` |

So: `2026-10-webapp-ui-ux.md`, with its captures in
`docs/audit/assets/2026-10-webapp-ui-ux/`.

The date prefix sorts the folder chronologically and keeps two audits of the
same scope from colliding — a second audit of the same topic simply gets the
next month.

## What a report contains

The shape is the same everywhere, so two audits can be read side by side:

1. **What was audited and how** — the surfaces, the method, and what was
   checked by reading the code against the WCAG criteria, not only by looking
   at the running app.
2. **A severity model** — stated before the findings, so a reader can judge
   the ordering. Define what a severity means *for this repo*.
3. **The findings** — a table with the finding, the evidence, where it is,
   and what changed. The last column is what makes a report worth keeping:
   it records what the fix was, not only what was wrong.
4. **The guardrails** — the permanent checks that keep the findings fixed.
   A finding whose fix can silently regress is a finding that is not fixed.
5. **The evidence** — how to regenerate the captures.
6. **The deferred backlog** — everything deliberately left for later, as
   linked issues rather than as prose. A deferred finding with no issue is a
   finding nobody will pick up.

The skeleton, so a new report starts from the same shape:

```markdown
# <Topic> audit — <scope>

**Scope.** What was audited, and what was explicitly out of scope.

**Status.** What was fixed, and where it lives.

---

## How the audit was run

## Severity model

## Fixed in this branch

| # | Finding | Where | Fix |
|---|---|---|---|

## Guardrails added

## Evidence

## Deferred backlog
```

## Running an audit

Captures are taken from an isolated demo instance, never from a live one:

```bash
make screenshots-audit AUDIT=2026-10-webapp-ui-ux
```

writes the phone and tablet captures of every page into
`docs/audit/assets/2026-10-webapp-ui-ux/`. The `AUDIT` value is the report's
slug — the same one its file name uses. Nothing outside that folder is
touched.

## Index

| Audit | Scope | Outcome |
|---|---|---|
| [2026-10-webapp-ui-ux.md](2026-10-webapp-ui-ux.md) | Webapp UI/UX — accessibility, mobile ergonomics, data honesty | 2 P0 and 24 P1 fixed; 4 further P1 found by the strengthened guard and fixed; guardrails added; the P2/P3 backlog filed as issues #948–#953 |
| [2026-10-sidecar-source-authorization-delivery.md](2026-10-sidecar-source-authorization-delivery.md) | Sidecar sources — the Sources consent model | Frozen snapshot of the delivery of #780; the durable reference is [`../source-consent.md`](../source-consent.md) |
| [2026-10-sidecar-source-renewal-readiness-781.md](2026-10-sidecar-source-renewal-readiness-781.md) | Sidecar sources — renewal readiness (#781) | Frozen snapshot: implemented mechanisms against the remaining acceptance gates |
| [2026-10-cicd-edge-release-recovery.md](2026-10-cicd-edge-release-recovery.md) | CI/CD — the edge release failures after #674 and #680 | Frozen incident record; the release choreography it describes is now [`../ci-cd.md`](../ci-cd.md) |

## Frozen snapshots

The last three rows are **not** maintained reports. They are point-in-time
records — an approved plan, a readiness inventory, an incident log — frozen at
the date they were written. Each carries a banner saying so.

> **If a claim in a frozen snapshot contradicts a current reference document,
> the reference document wins.** These files record what was true on their date,
> not what is true now.

| Frozen record | Superseded by |
|---|---|
| `2026-10-sidecar-source-authorization-delivery.md` | [`../source-consent.md`](../source-consent.md) — the Sources consent model, who grants it, and what withdrawal does |
| `2026-10-sidecar-source-renewal-readiness-781.md` | [`../collectors/claude.md`](../collectors/claude.md) — the renewal capability split |
| `2026-10-cicd-edge-release-recovery.md` | [`../ci-cd.md`](../ci-cd.md) — the release and signing choreography |

## Acceptance evidence is the exception

The rule above — *the report is committed, the evidence stays local* — applies
to **captures**: screenshots and videos of a running app, which are
regenerable from a demo instance and carry permanent history weight.

It does **not** apply to the acceptance records in
[`../evidence/`](../evidence/). Those are the recorded outcomes of past
verification runs. They are not regenerable — the run happened once — and six
of them are cited from live issues. They stay in the repository.
