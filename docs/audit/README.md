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
