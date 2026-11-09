# Runway documentation

Start with the project [README](../README.md) for what Runway does and how to
run it. This index covers everything else.

| If you want to… | Read |
|---|---|
| Install and run Runway | [deployment.md](deployment.md) · [sidecar.md](sidecar.md) |
| Fix something that is not working | [troubleshooting.md](troubleshooting.md) |
| Put it behind SSO or a reverse proxy | [forward-auth.md](forward-auth.md) · [SECURITY.md](SECURITY.md) |
| Understand how it works | [architecture.md](architecture.md) · [collection_logic.md](collection_logic.md) |
| Add or fix a provider | [collectors/README.md](collectors/README.md) |
| Call the API | [api-reference.md](api-reference.md) |
| Contribute code | [../CONTRIBUTING.md](../CONTRIBUTING.md) · [ci-cd.md](ci-cd.md) |

## Using Runway

| Document | What it covers |
|---|---|
| [deployment.md](deployment.md) | Running the server and its sidecars: Python, Docker, Traefik, the environment variables that matter. |
| [sidecar.md](sidecar.md) | The sidecar: install, CLI, configuration, updates. |
| [troubleshooting.md](troubleshooting.md) | Symptom-driven fixes. |
| [migration-v3.md](migration-v3.md) | Upgrading a 2.x database to 3.x. |
| [data-health.md](data-health.md) | The in-app checks and repairs. |
| [maintenance-scripts.md](maintenance-scripts.md) | Command-line equivalents of the in-app repairs. |
| [source-consent.md](source-consent.md) | Which local sources a sidecar may read, who grants that, and what withdrawal does. |
| [macos-usage.md](macos-usage.md) | macOS-specific usage collection. |

## Security

| Document | What it covers |
|---|---|
| [SECURITY.md](SECURITY.md) | The trust model: what is verified, encrypted and required of an operator. |
| [forward-auth.md](forward-auth.md) | SSO and forward-auth, with an Authentik + Traefik walkthrough. |
| [security/README.md](security/README.md) | The release-image policy and its validation evidence. |

## Reference

| Document | What it covers |
|---|---|
| [architecture.md](architecture.md) | The event-sourced data model, the ingest path, the service modules and the collectors. |
| [collection_logic.md](collection_logic.md) | How a collector turns a response into a card: strategies, enrichment, reconciliation. |
| [statistics.md](statistics.md) | The `data_source` / `input_source` values every card carries. **The source of truth for those values.** |
| [api-reference.md](api-reference.md) | Every `/api/v1` route, its auth, and the `LimitCard` schema. |
| [fleet-protocol.md](fleet-protocol.md) | The sidecar↔server protocol: ingest auth, pairing, credential manifests, remote control. |
| [claude-cloud-event-contract.md](claude-cloud-event-contract.md) | The specification the Claude cloud-runner collector is built against. |
| [frontend.md](frontend.md) | The React app: feature map, data-fetching conventions, the `LimitCard` mirror. |
| [collectors/README.md](collectors/README.md) | Every provider: how it collects, what credential it needs, and its own document. |

## Developing Runway

| Document | What it covers |
|---|---|
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | The contributor workflow. |
| [../AGENTS.md](../AGENTS.md) | The conventions and invariants for anyone, human or agent, changing this repository. |
| [ci-cd.md](ci-cd.md) | The workflows, the gates they enforce, and the release process. |
| [sidecar-build.md](sidecar-build.md) | Building and packaging the sidecar. |
| [demo.md](demo.md) | The throwaway demo instance and the screenshot pipeline. |
| [branding.md](branding.md) | Icons, splash screens and the `make logo` pipeline. |
| [security-validation.md](security-validation.md) | How the security gates work. |
| [server-memory.md](server-memory.md) · [sidecar-memory.md](sidecar-memory.md) | How memory use behaves, and how to measure it. |

## Point-in-time records

Some material here is **frozen**. It is not maintained, and where a record
contradicts a reference document above, the reference document wins.

| Directory | What it holds |
|---|---|
| [evidence/](evidence/README.md) | Append-only acceptance and verification records named after the issue they evidence. They hold booleans, counts and statuses, never credentials or account identifiers. |
| [audit/](audit/README.md) | Audit reports named `YYYY-MM-<scope>-<topic>.md`. The UI/UX audit is maintained; the other two are frozen snapshots and say so in their banner. |

## A note on dates

No document here carries a "Last updated" line. Git history is the record of
when a document changed: `git log -1 --format=%cs -- <file>` gives the date of
its most recent edit.
