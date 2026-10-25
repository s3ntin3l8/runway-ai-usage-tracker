# Runway documentation

Everything about Runway that is not the code itself. Start here.

| If you want to… | Read |
|---|---|
| Understand the system | [architecture.md](architecture.md) |
| Know what the API does | [api-reference.md](api-reference.md) |
| Set it up and run it | [deployment.md](deployment.md) · [sidecar.md](sidecar.md) |
| Understand the security model | [SECURITY.md](SECURITY.md) · [forward-auth.md](forward-auth.md) |
| Add or fix a provider | [collectors/README.md](collectors/README.md) |
| Contribute code | [../CONTRIBUTING.md](../CONTRIBUTING.md) · [ci-cd.md](ci-cd.md) |
| Work on the frontend | [frontend.md](frontend.md) |
| Debug something | [troubleshooting.md](troubleshooting.md) |

## Reference

| Document | What it is |
|---|---|
| [architecture.md](architecture.md) | The event-sourced data model, the ingest path, the service modules and the collectors. |
| [api-reference.md](api-reference.md) | Every `/api/v1` route, its auth scheme, and the `LimitCard` schema. |
| [collection_logic.md](collection_logic.md) | How a collector turns a response into a card — strategies, enrichment, and card reconciliation. |
| [statistics.md](statistics.md) | The `data_source` / `input_source` taxonomy. **The source of truth for those values.** |
| [fleet-protocol.md](fleet-protocol.md) | The sidecar↔server protocol: ingest auth, pairing, credential manifests, pending events, remote control. |
| [frontend.md](frontend.md) | The React app: feature map, data-fetching conventions, and the `LimitCard` mirror contract. |
| [demo.md](demo.md) | The throwaway demo instance: `make demo`, `make screenshots`, and the isolation rules it runs under. |
| [source-consent.md](source-consent.md) | Which local sources a sidecar may read, who grants that, and what happens on withdrawal. |
| [data-health.md](data-health.md) | The in-app checks and repairs, and the endpoint behind them. |
| [maintenance-scripts.md](maintenance-scripts.md) | The command-line equivalents of the in-app repairs. |
| [migration-v3.md](migration-v3.md) | Upgrading a 2.x database to the 3.x schema. |
| [sidecar.md](sidecar.md) | The sidecar: install, CLI, configuration, protocol internals, troubleshooting. |
| [sidecar-build.md](sidecar-build.md) | Building and packaging the sidecar: PyInstaller specs, NSIS, signing, update budgets. |
| [server-memory.md](server-memory.md) | How the server's memory use behaves, and how to measure it. |
| [sidecar-memory.md](sidecar-memory.md) | The same for the sidecar. |
| [macos-usage.md](macos-usage.md) | macOS-specific usage collection behaviour. |
| [branding.md](branding.md) | Icons, splash screens and the `make logo` pipeline. |

## Operations

| Document | What it is |
|---|---|
| [deployment.md](deployment.md) | Running the server and its sidecars — Docker, compose, networks, and the environment-variable contract. |
| [ci-cd.md](ci-cd.md) | The workflows, the gates they enforce, and the release choreography. |
| [troubleshooting.md](troubleshooting.md) | Symptom-driven fixes. |
| [security-validation.md](security-validation.md) | How the security gates work, and the dated results of the last run. |

## Security

| Document | What it is |
|---|---|
| [SECURITY.md](SECURITY.md) | The trust model: what is verified, what is encrypted, what an operator must configure. |
| [forward-auth.md](forward-auth.md) | The SSO / forward-auth trust boundary, with an Authentik + Traefik walkthrough. |
| [security/README.md](security/README.md) | The release-image policy and the dated validation evidence. |

## Providers

[collectors/README.md](collectors/README.md) maps every provider to its
collector module, its registry rules and its own document. Two sources —
`hermes` and `minimax_code` — are **sidecar-only**: they produce usage events,
never quota cards, and the server does not list them as providers.

## Point-in-time records

Some material here is **frozen**. It is not maintained, and where a record
contradicts a reference document, the reference document wins.

| Directory | What it holds |
|---|---|
| [evidence/](evidence/README.md) | Acceptance and verification records from superpowers-driven PR runs, named after the issue they evidence. They record booleans, counts and statuses — never credential values or account identifiers. |
| [audit/](audit/README.md) | Audit reports, each named `YYYY-MM-<scope>-<topic>.md`. Three of them are frozen snapshots — an approved plan, a readiness inventory and an incident log — and each says so in its own banner. |

## Related

- [../README.md](../README.md) — the project README.
- [../AGENTS.md](../AGENTS.md) — the rules for working in this repository.
- [../CONTRIBUTING.md](../CONTRIBUTING.md) — how to contribute.

## A note on dates

No document here carries a "Last updated" line. Git history is the record of
when a document changed: `git log -1 --format=%cs -- <file>` gives the date of
its most recent edit. An in-file date goes stale the moment the next edit
lands, and it cannot describe the part of the file that matters most — the part
that is wrong.
