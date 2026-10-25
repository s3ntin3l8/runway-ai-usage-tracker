# The demo instance

`scripts/demo/` builds a **throwaway Runway** with synthetic data. It exists for
two jobs: showing the product to someone who should not see your real accounts,
and regenerating the screenshots that ship in the README and on the landing page.

```bash
make demo                 # serve a demo instance until Ctrl-C, then delete everything
make screenshots          # regenerate the README + PWA manifest screenshots
make screenshots-landing OUT=./landing-shots   # landing-page stills and a short loop
make screenshots-audit AUDIT=2026-10-webapp-ui-ux   # phone + tablet stills of every page
```

Nothing here is production code. It never talks to a provider, never reads a
real credential, and never touches a real database — see
[Isolation guarantees](#isolation-guarantees).

## The commands

| Command | Module | What it does |
|---|---|---|
| `make demo` | `scripts/demo/instance.py` | Seeds a scratch database, starts the server on a loopback port, and serves it until you press Ctrl-C. The scratch directory is deleted on the way out. |
| `make screenshots` | `scripts/demo/capture.py` | Boots the same instance, runs the Playwright driver `webapp/scripts/screenshots.mjs` against it, and tears it down. The `readme` profile captures the README set (dark and light) and the PWA manifest images. |
| `make screenshots-landing OUT=<dir>` | `scripts/demo/capture.py` | The `landing` profile. Writes landing-page stills and a short video loop into `OUT` (default `landing-shots`). The loop needs `ffmpeg` on `PATH`. |
| `make screenshots-audit AUDIT=<slug>` | `scripts/demo/capture.py` | The `audit` profile. Writes phone and tablet stills of every page, dark and light, into `docs/audit/assets/<slug>/`. The slug is the audit's own name — the same one its report file uses — so two audits never overwrite each other's captures. The captures are **gitignored**: the report is the committed artifact, the images stay local. |

All three capture profiles are implemented in `webapp/scripts/screenshots.mjs`,
which reads `SCREENSHOTS_PROFILE` (`readme`, `landing` or `audit`).
`make screenshots-landing` requires an explicit output directory — the capture
script rejects the landing profile without one rather than writing into the
repository by surprise.

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

`scripts/demo/seed_demo.py` writes a synthetic dataset into the scratch
database. It refuses to run against any database other than the scratch one.

The data is generated relative to *now* from a fixed random seed, so a capture
always looks current and repeated runs produce the same shapes.

| Provider | Cards | Notes |
|---|---|---|
| Anthropic | `anthropic_cards()` | Claude subscription quotas |
| ChatGPT | `chatgpt_cards()` | |
| Gemini | `gemini_cards()` | |
| GitHub | `github_cards()` | Copilot quotas |
| OpenCode | `opencode_cards()` | |
| Ollama | `ollama_cards()` | |

Every seeded account also gets a `ProviderConfig` row, because Runway's
data-health checks flag gauge data that has no configuration behind it.

Usage events go through Runway's own code paths wherever they exist: limit
cards come from each collector's own parse function fed a synthetic provider
response, and usage events go through the `EventIngestor`, which prices them and
maintains the period rollups. Past quota windows are closed with
`close_window`. Two synthetic sidecars are registered in the fleet registry
(`demo-mbp`, `demo-workstation`) so the fleet view is not empty.

## Lifecycle

`start_demo()` in `scripts/demo/instance.py` is the whole lifecycle:

1. Assert the port is free.
2. Build the SPA if `webapp/dist` is missing or older than its sources
   (`ensure_spa_built()`).
3. Create a `mkdtemp` scratch directory, lock it, and build the isolated
   environment. The lock is held for as long as the demo runs, so a later
   demo can tell a live scratch dir from a leftover one.
4. Seed the database.
5. Start the server in the demo's own process group and wait for it to answer
   HTTP.

`Demo.stop()` terminates the server by name — the server shares the demo's
process group rather than leading one of its own, so a supervisor that
stops the demo takes the server with it — then deletes the scratch
directory. Pass `keep=True` to keep it for inspection; the printed path is
where it landed, and the dir says so itself, so the next demo's sweep leaves
it alone.

## Related Files

| File | Role |
|---|---|
| `scripts/demo/instance.py` | Boots and tears down the instance; owns the scratch directory, the port and the SPA build. |
| `scripts/demo/env.py` | Builds and validates the isolated environment. |
| `scripts/demo/seed_demo.py` | Writes the synthetic dataset. |
| `scripts/demo/capture.py` | Drives the Playwright screenshot run against a demo instance. |
| `webapp/scripts/screenshots.mjs` | The Playwright driver; implements the `readme` and `landing` profiles. |
| `Makefile` | The `demo`, `screenshots` and `screenshots-landing` targets. |

## References

- [`frontend.md`](frontend.md) — the app the demo serves, and the screenshot
  pipeline it feeds.
- [`branding.md`](branding.md) — the brand surfaces the README screenshots show.
- [`security-validation.md`](security-validation.md) — the gates a demo instance
  is subject to.
