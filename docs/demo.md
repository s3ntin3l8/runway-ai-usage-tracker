# The demo instance

`scripts/demo/` builds a **throwaway Runway** with synthetic data. It exists for
two jobs: showing the product to someone who should not see your real accounts,
and regenerating the screenshots that ship in the README and on the landing page.

```bash
make demo                 # serve a demo instance until Ctrl-C, then delete everything
DEMO_NOW=2026-10-15T10:00:00Z make demo   # the same, with the clock fixed
make screenshots          # regenerate the README + PWA install-dialog screenshots (Docker)
make screenshots-landing OUT=./landing-shots   # landing-page stills and a short loop
make screenshots-audit AUDIT=2026-10-webapp-ui-ux   # desktop, tablet and phone stills of every page
make visual-check ROUTES=history   # before/after of the pages a UI change touches
make visual-baseline      # regenerate the committed baselines
```

Nothing here is production code. It never talks to a provider, never reads a
real credential, and never touches a real database — see
[Isolation guarantees](#isolation-guarantees).

## The commands

| Command | Module | What it does |
|---|---|---|
| `make demo` | `scripts/demo/instance.py` | Seeds a scratch database, starts the server on a loopback port, and serves it until you press Ctrl-C. The scratch directory is deleted on the way out. |
| `make screenshots` | `visual/` | Renders the `presentation` scenes of `visual/scenes.json` against the demo (README profile: dark and light, desktop and mobile) into `docs/screenshots/`, then copies the PWA install-dialog images listed in `webapp/pwa-screenshots.json` into `webapp/public/screenshots/`. |
| `make screenshots-landing OUT=<dir>` | `visual/` | The landing profile: dark stills at 1600×1000 @2x and 390×844 @3x, plus the `provider-tabs` loop (webm, mp4, jpg poster). `make screenshots-landing` writes into `OUT` (default `landing-shots`), which personal-landing's `npm run assets:sync runway` reads. |
| `make screenshots-audit AUDIT=<slug>` | `visual/` | The audit profile: desktop, tablet and phone stills of every page, dark and light, into `docs/audit/assets/<slug>/`. The slug is the audit's own name — the same one its report file uses — so two audits never overwrite each other's captures. The captures are **gitignored**: the report is the committed artifact, the images stay local. |
| `make visual-check [ROUTES=a,b] [BASE=ref]` | `visual/` | Renders the merge base and the working tree and reports what changed (`visual/.out/check/report/`). `REPEAT=2` also captures the working tree again and fails when the two captures differ. |
| `make visual-baseline` | `visual/` | Regenerates `visual/baselines/`, the committed check shots of the scenes flagged `"baseline": true`. |

The four capture targets run in the [visual-kit](https://github.com/s3ntin3l8/visual-kit)
image pinned in `visual/Dockerfile` (Docker is required). It starts `make demo`
with `DEMO_NOW` set, waits for the `DEMO_READY` line, and drives the pages from
`visual/scenes.json`; `visual/hooks.mjs` holds what is specific to Runway (see
[Screenshots and visual checks](#screenshots-and-visual-checks)).

## Isolation guarantees

The demo usually runs on a host that *also* runs a real Runway: a container, a
`make run` deployment, a developer shell full of exported secrets. The demo
never shares state with any of them.

`scripts/demo/env.py` builds every demo process's environment from scratch:

- **Only `PATH`, `LANG`, `LC_ALL`, `TERM` and `TZ` are inherited.** Nothing else
  from the shell reaches the demo.
- **`HOME`, `TMPDIR`, `RUNWAY_CONFIG_DIR`, `XDG_CONFIG_HOME` and
  `XDG_DATA_HOME` all point inside one scratch directory.** Collectors that look
  in `HOME` for CLI logins, keychains and browser cookies therefore find
  nothing.
- **The server runs from a scratch working directory with no `.env`.**
  `pydantic-settings` loads `.env` from the working directory, so a checkout
  carrying real provider keys, `ADMIN_API_KEY`, `INGEST_API_KEY` or a
  `GITHUB_TOKEN` would otherwise be read.
- **No secrets are set at all.** The demo generates no keys of its own.
- **`TRUSTED_PROXY_IPS` and `PUBLIC_URL` are forbidden** — they would change
  how the server trusts the network.

`assert_demo_env()` re-checks the finished environment before the server
starts and refuses to boot if any path-shaped variable still points outside the
scratch directory. The same check runs again inside the capture process.

Two more guards apply:

- **The demo never takes a real instance's port.** `8765` is reserved and
  refused; the demo defaults to `3965` and asserts that the port is free before
  starting. Override it with `DEMO_PORT`.
- **The demo never touches Docker.** A compose deployment on the same host is
  out of reach — start the demo while nothing else claims the port.

## What gets seeded

`scripts/demo/seed_demo.py` writes a synthetic dataset (Anthropic, ChatGPT,
Gemini, GitHub, OpenCode and Ollama accounts) into the scratch database and
refuses to run against any other database. The data is generated relative to
*now* from a fixed random seed, so a capture looks current and repeated runs
produce the same shapes.

Limit cards come from each collector's own parse function fed a synthetic
provider response; usage events go through the `EventIngestor`, which prices
them and maintains the rollups; past quota windows are closed with
`close_window`. Every account gets a `ProviderConfig` row so data-health checks
do not flag it, and two synthetic sidecars (`demo-mbp`, `demo-workstation`) are
registered so the fleet view is not empty.

## Lifecycle

`start_demo()` in `scripts/demo/instance.py` asserts the port is free, builds
the SPA if `webapp/dist` is missing or stale, creates a locked scratch
directory with the isolated environment, seeds it, starts the server and waits
for HTTP. `make demo` then prints `DEMO_READY url=…`, the line visual-kit waits
for. `Demo.stop()` terminates the server and deletes the scratch directory
(`keep=True` keeps it for inspection).

With `DEMO_NOW` set (an ISO date-time such as `2026-10-15T10:00:00Z`; a bare
date is refused), the seed and the server run through
`scripts/demo/freeze_time.py`, which starts their clock at that instant with
`time-machine`. It keeps ticking from there, so timeouts and the rate limiter
still behave, and relative times ("resets in 1h 25m") read the same on any day.

## Related Files

| File | Role |
|---|---|
| `scripts/demo/instance.py` | Boots and tears down the instance; owns the scratch directory, the port and the SPA build. |
| `scripts/demo/env.py` | Builds and validates the isolated environment. |
| `scripts/demo/seed_demo.py` | Writes the synthetic dataset. |
| `scripts/demo/freeze_time.py` | Runs the seed and the server with the clock fixed at `DEMO_NOW`. |
| `visual/scenes.json`, `visual/hooks.mjs` | The pages visual-kit captures, and the app-specific settle logic. |
| `visual/Dockerfile` | Pins the visual-kit image; Dependabot bumps it. |
| `visual/baselines/` | The committed check shots of the baseline scenes. |
| `scripts/sync-pwa-screenshots.mjs`, `webapp/pwa-screenshots.json` | Copy the PWA manifest's screenshots into `webapp/public/screenshots/`. |
| `.github/workflows/visual*.yml` | The visual check, the weekly refresh and `/update-baselines`. |
| `Makefile` | The `demo`, `screenshots*`, `visual-check` and `visual-baseline` targets. |

## Screenshots and visual checks

`visual/scenes.json` lists the pages (dashboard, provider, provider activity,
history, fleet, settings) and the `provider-tabs` loop; each scene is tagged
`presentation` (README and landing), `check` and `audit`. The demo's clock is
fixed in two places that must agree: `DEMO_NOW` for the server and `clock` for the
browser.

`visual/hooks.mjs` is what the capture needs from the app, and each part has a
reason:

- **API calls wait for the fonts.** The charts are ECharts canvases, laid out from
  text widths measured when they first render; a late web font gave a different
  legend from run to run.
- **Charts are drawn without animation.** `EChart` honours `prefers-reduced-motion`
  (visual-kit emulates it), so a chart is in its final state at once.
- **The page must be steady.** No skeletons, setup banners dismissed, every box
  and every canvas unchanged for 1.5 s, and the page scrolled once so nothing
  below the fold is still starting.
- **Focus is dropped after a click**, so a clicked tab does not carry a focus
  ring a mouse user would not see.

`make visual-check REPEAT=2` on an unchanged tree must report no determinism
failure. The one known allowance is the history scene (`maxDiffPixels: 50`): its
time axis starts at the server's `now − 7 days`, the server clock keeps ticking, so
the first plotted sample flips every 15 minutes of uptime and moves the axis by a
few pixels.

The **baselines** (`dashboard`, `provider` and `history`, in `visual/baselines/`)
are committed so a reviewer sees the visual change in the pull request. Comment
`/update-baselines` on a PR (write access, same-repository branch) and CI
regenerates them and pushes one commit; or run `make visual-baseline` and commit.

Two things keep the committed images from going stale: the **visual check** runs
on UI pull requests and posts its report as a comment (advisory), and
`.github/workflows/visual-refresh.yml` runs every Monday, regenerating the
README and PWA screenshots and the baselines. When an image changed by more than
the tolerance it opens or updates one pull request each (`bot/refresh-screenshots`,
`bot/refresh-baselines`); review the images and merge or close it.

## References

- [`frontend.md`](frontend.md) — the app the demo serves, and the screenshot
  pipeline it feeds.
- [`branding.md`](branding.md) — the brand surfaces the README screenshots show.
- [`security-validation.md`](security-validation.md) — the gates a demo instance
  is subject to.
