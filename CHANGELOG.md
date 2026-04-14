# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.10.1](https://github.com/s3ntin3l8/ai-usage-tracker/compare/v0.10.0...v0.10.1) (2026-04-14)


### Bug Fixes

* **ci:** trigger build-and-push on release published event ([9e579db](https://github.com/s3ntin3l8/ai-usage-tracker/commit/9e579db8cf8542e3f5f52ea737cc8dfef06f33d4))

## [0.10.0](https://github.com/s3ntin3l8/ai-usage-tracker/compare/v0.9.0...v0.10.0) (2026-04-14)


### Features

* **ci:** add sidecar-release workflow to build PyInstaller binaries on version tags ([5074c0f](https://github.com/s3ntin3l8/ai-usage-tracker/commit/5074c0f1dc47b6f7657f374940dcc066efb7155a))
* Feature 5D — native desktop sidecar (macOS menubar + Windows tray) ([1dcc7a3](https://github.com/s3ntin3l8/ai-usage-tracker/commit/1dcc7a30aedc99bbdaeed4d80263561d7a54432c))
* Feature 5D — native desktop sidecar (macOS menubar + Windows tray) ([1dcc7a3](https://github.com/s3ntin3l8/ai-usage-tracker/commit/1dcc7a30aedc99bbdaeed4d80263561d7a54432c))
* **frontend:** add code splitting with separate view modules ([3d33df2](https://github.com/s3ntin3l8/ai-usage-tracker/commit/3d33df2cf8dba8fac4d959be11b7bb54a8aa28e2))
* **frontend:** extract dashboard into separate lazy-loaded module ([7593478](https://github.com/s3ntin3l8/ai-usage-tracker/commit/7593478c20279787e0ca8b879d26c8d940a32ad9))
* **frontend:** extract Fleet view into separate module ([4fbd5a8](https://github.com/s3ntin3l8/ai-usage-tracker/commit/4fbd5a865e3163c6020a403172c81cbfde97ebcd))
* **sidecar-app:** add autostart login-item install for macOS and Windows ([89dae97](https://github.com/s3ntin3l8/ai-usage-tracker/commit/89dae974b14733a180c3fd2999ff0ecaf235d322))
* **sidecar-app:** add GitHub Releases update checker ([52b49d7](https://github.com/s3ntin3l8/ai-usage-tracker/commit/52b49d77d34ccbefe40d426eafb498d54cf3a05d))
* **sidecar-app:** add PyInstaller spec files for macOS and Windows ([94fe008](https://github.com/s3ntin3l8/ai-usage-tracker/commit/94fe0084c1b5dc32e3b185372ef917b53094caf3))
* **sidecar-app:** add sidecar_app package skeleton with tray, daemon, and config modules ([8668210](https://github.com/s3ntin3l8/ai-usage-tracker/commit/866821052b38b360f3353526956987fe9dfd77ca))
* **sidecar-app:** wire first-run notification and menu action callbacks ([17a14a2](https://github.com/s3ntin3l8/ai-usage-tracker/commit/17a14a20d107c98f3190b380859fef5246c5872b))
* **ui:** add buildProviderSummaryCard component ([52e028c](https://github.com/s3ntin3l8/ai-usage-tracker/commit/52e028c3f40a8446b7505cc56a0c7fab67582fd6))
* **ui:** add health overview bar to dashboard ([00ac8e3](https://github.com/s3ntin3l8/ai-usage-tracker/commit/00ac8e3f0bcb3e4afb8c9a238741511fdf5b41a2))
* **ui:** add provider drill-down modal with sparklines ([691139c](https://github.com/s3ntin3l8/ai-usage-tracker/commit/691139c712d28d08c577a8f1e3d41155d5830889))
* **ui:** add provider sparkline strip to history tab ([792265d](https://github.com/s3ntin3l8/ai-usage-tracker/commit/792265de19bb0bec3eb6ba03a807a2b4084caf3c))
* **ui:** add time range, metric switcher, and CSV filter to history tab ([50affd5](https://github.com/s3ntin3l8/ai-usage-tracker/commit/50affd58777756e2862ed8f4bb79564835bbf275))
* **ui:** add Window filter dimension to dashboard filter bar ([97ea70f](https://github.com/s3ntin3l8/ai-usage-tracker/commit/97ea70f8c98d406d01209360541f6e5b5d5dcd11))
* **ui:** wire provider summary cards into dashboard grid ([09ac40f](https://github.com/s3ntin3l8/ai-usage-tracker/commit/09ac40f221c089f1b6dc20a25cf5ca297790fcb6))


### Bug Fixes

* address post-redesign UI and backend issues ([ae0bd82](https://github.com/s3ntin3l8/ai-usage-tracker/commit/ae0bd822bdb9d258dbc3c6365602ada4bb288e9c))
* **collector:** fix Kimi and MiniMax providers with new features ([a747c80](https://github.com/s3ntin3l8/ai-usage-tracker/commit/a747c80db1278435469a36e02660b0336d77bd74))
* **collector:** improve Anthropic and related collectors ([9ad1782](https://github.com/s3ntin3l8/ai-usage-tracker/commit/9ad1782c035dcb804c0416f7066bde1f9f75549a))
* **collector:** improve GitHub Copilot collector ([8200bcf](https://github.com/s3ntin3l8/ai-usage-tracker/commit/8200bcf194c8cadcf34af18526759bb262aaa521))
* **collector:** improve Ollama and other collectors ([5cb30fd](https://github.com/s3ntin3l8/ai-usage-tracker/commit/5cb30fd7f73c738d9bd636e603b939321246fdc9))
* **collector:** improve OpenRouter and OpenCode collectors ([a748c1c](https://github.com/s3ntin3l8/ai-usage-tracker/commit/a748c1c62aa6412683097ac751e53ba63baad523))
* **collector:** merge zAI collectors into single provider ([3c48eb7](https://github.com/s3ntin3l8/ai-usage-tracker/commit/3c48eb7279b129249c5e7351c3e68ef531f87587))
* **core:** improve backend services and configuration ([ccf4630](https://github.com/s3ntin3l8/ai-usage-tracker/commit/ccf46302ac694117225f7a08f2bc0ed044d9a0ea))
* font sizes, polling, filter default, GitHub identity, bright mode ([6111da7](https://github.com/s3ntin3l8/ai-usage-tracker/commit/6111da76184acbceb40942d1fec7d059bdd6fd23))
* **frontend:** improve settings UI and components ([ab12f07](https://github.com/s3ntin3l8/ai-usage-tracker/commit/ab12f071b1906adaa0f374b950dca6c4e4b900c1))
* history fractional days, Anthropic windows/tier, readability, modal ([4139414](https://github.com/s3ntin3l8/ai-usage-tracker/commit/413941417b7745672690f90e5c5e793fc1c3b53e))
* **lint:** apply ruff format to all files and fix noqa placement ([350108d](https://github.com/s3ntin3l8/ai-usage-tracker/commit/350108dee05ec0cb873b7c0c694cdb4d8c9db8a3))
* **lint:** resolve all ruff warnings across codebase ([672603e](https://github.com/s3ntin3l8/ai-usage-tracker/commit/672603e262d8b5b8d504f891ab49a4bd72191828))
* **lint:** resolve mypy errors blocking CI ([3455a6a](https://github.com/s3ntin3l8/ai-usage-tracker/commit/3455a6ac23bf51a57acb5ca94560d72d625a1f3b))
* remove stale REFRESH_CONFIG and setChartView imports that broke app module load ([e5fa6b6](https://github.com/s3ntin3l8/ai-usage-tracker/commit/e5fa6b6acab1a06dcc4ad4c59a45a8eef44dc014))
* **server:** always read index.html from disk, remove in-memory HTML cache ([8195767](https://github.com/s3ntin3l8/ai-usage-tracker/commit/8195767bc2ee3bf827fb3390fee37235dcb164c1))
* **sidecar-app:** remove invalid onefile=True from windows.spec EXE ([c27daab](https://github.com/s3ntin3l8/ai-usage-tracker/commit/c27daab24f54ba06dc8b07bc1d1a610ec619d8ec))
* **sidecar-app:** silence OSError in _windows_install for locked-down registry ([352e329](https://github.com/s3ntin3l8/ai-usage-tracker/commit/352e3295dde32a19836023f5a1bab7a58d802197))
* **sidecar-app:** use sys._MEIPASS for frozen PyInstaller path resolution ([6f06680](https://github.com/s3ntin3l8/ai-usage-tracker/commit/6f066803426a3dee3ae6081005c8757559159f08))
* **sidecar-app:** use xdg-open from PATH instead of hardcoded /usr/bin ([52db149](https://github.com/s3ntin3l8/ai-usage-tracker/commit/52db1497c1fd04000ca5e64af40f6c1bd071f3da))
* **tests:** remove unused imports and sort import block in test_sidecar_autostart ([fa1f9f2](https://github.com/s3ntin3l8/ai-usage-tracker/commit/fa1f9f20041cadd6beb7480356348a24a412148a))
* **tests:** update collector tests ([65b7e22](https://github.com/s3ntin3l8/ai-usage-tracker/commit/65b7e2295a62bb750aeed16479cb7ed5cc9c749b))
* **test:** update registry count assertion to 13 (added kimi_k2) ([635d971](https://github.com/s3ntin3l8/ai-usage-tracker/commit/635d9714ecd840b4a84bbf45051cb63871e508af))
* **test:** use tuple for RECOGNIZED_COOKIE_NAMES (frozenset is unordered) ([06ef093](https://github.com/s3ntin3l8/ai-usage-tracker/commit/06ef0931bfcc8161c8dda39f45e234ed1e510428))
* **ui:** add daily to window filter sort order (session→daily→weekly→biweekly→monthly→prepaid) ([a7b8583](https://github.com/s3ntin3l8/ai-usage-tracker/commit/a7b85834152b33fdad9995062ca05bb095661d8f))
* **ui:** apply card-layout to buildProviderSummaryCard (the actual dashboard card) ([3cf2c99](https://github.com/s3ntin3l8/ai-usage-tracker/commit/3cf2c99c76f6feb0190b4ca885413b545147f733))
* **ui:** bump modal detail text to ~150% — labels text-xl, values text-2xl ([7e99bea](https://github.com/s3ntin3l8/ai-usage-tracker/commit/7e99bea440f599a92e2806e1a6520c0d34926e23))
* **ui:** card bottom shading, larger modal text, TZ display, and identity fixes ([a5ff2b8](https://github.com/s3ntin3l8/ai-usage-tracker/commit/a5ff2b8c7c33d2651f32e37bde9d7af273472022))
* **ui:** card shading extends to bottom via grid-template-rows layout ([57b8a4c](https://github.com/s3ntin3l8/ai-usage-tracker/commit/57b8a4c02b2be27f13ffa204a60b08b27b66626f))
* **ui:** double font sizes in provider group modal service rows ([921d340](https://github.com/s3ntin3l8/ai-usage-tracker/commit/921d34054c033219496c3864575a9334026fb87c))
* **ui:** escape formatResetDisplay output and validate sparkline numeric values ([13928db](https://github.com/s3ntin3l8/ai-usage-tracker/commit/13928dbb31be5a6990b03db80b640066f05fda83))
* **ui:** health bar empty-state guard and bar segment border-radius ([8e6c7a0](https://github.com/s3ntin3l8/ai-usage-tracker/commit/8e6c7a0321efa21a7ea4fb911a29ac07cc7e2b30))
* **ui:** increase font sizes in provider group modal (buildProviderModal) ([40e0a4a](https://github.com/s3ntin3l8/ai-usage-tracker/commit/40e0a4a2ec932a073033a4f02d4b4c03ff34f383))
* **ui:** larger modal text and matched health/tier badge style ([ab68dba](https://github.com/s3ntin3l8/ai-usage-tracker/commit/ab68dba348a49cfd08a11f13971c80ec22dc0a9e))
* **ui:** metric switcher filter sync, percent bucketing correctness, unknown provider handling ([6b0d96b](https://github.com/s3ntin3l8/ai-usage-tracker/commit/6b0d96bf280015442c4fd43b6c7ab9bd73dc3562))
* **ui:** provider card remaining coercion and tier badge from worst item ([6398b51](https://github.com/s3ntin3l8/ai-usage-tracker/commit/6398b51e75dc38893c6b5cdfd195cd5aae2f61a2))
* **ui:** remove redundant icon escaping and guard service_name null in history table ([593eb94](https://github.com/s3ntin3l8/ai-usage-tracker/commit/593eb94f98d411cb39ebcf4d35975629eb2e1f61))
* **ui:** reorder filter buttons Window→Account→Source, sort window pills logically ([a6b6357](https://github.com/s3ntin3l8/ai-usage-tracker/commit/a6b6357f0819ab502e40bca0aca37aaa19e173ef))
* **ui:** swap card-layout to auto/1fr so top zone keeps natural height ([8a66125](https://github.com/s3ntin3l8/ai-usage-tracker/commit/8a661252aed6107c33fc5f1fdbe032f45aa08c96))
* **ui:** use escapeHTML for text content in provider modal, harden health sort ([3709f43](https://github.com/s3ntin3l8/ai-usage-tracker/commit/3709f43be81e78dcf2f196d977a2fe943acc6c99))


### Performance Improvements

* **frontend:** add modal skeleton loading and history API caching ([d390202](https://github.com/s3ntin3l8/ai-usage-tracker/commit/d390202de4488f6556cdb1d35057295a59594f12))
* **frontend:** optimize initial page load performance ([1a88520](https://github.com/s3ntin3l8/ai-usage-tracker/commit/1a885201bdf675ccec0770dc9bc24acb8cbd54ed))

## [0.9.0] - 2026-04-13

### Added
- **Chart.js Visualizations (Phase 5A)**: Interactive token volume charts on the History tab with stacked bar and line toggles.
- **CSV Export (Phase 5B)**: Ability to download usage history as CSV via the UI and `/api/v1/usage/history?format=csv`.
- **Webhook Alerts (Phase 5B)**: Full CRUD API and background monitoring for usage breaches with Discord and Slack support.
- **Smart Polling (Phase 5C)**: Implemented "sleep mode" — 2h interval after 45min of no quota change to optimize resource usage.
- **Data Retention & Compaction (Phase 4.5)**: Background compaction service (60d hourly, 180d daily) to manage database growth.
- **Pydantic Settings (Phase 4.5)**: Migrated configuration to `pydantic-settings` for more robust environment variable handling.
- **Structured JSON Logging (Phase 4.5)**: Added `JsonFormatter` for `LOG_FORMAT=json` support, ideal for containerized environments.
- **Fleet Registry & Token Health (Phase 4)**: Improved fleet management, status tracking for multi-host deployments, and real-time health checks for cached credentials.
- **Instant-Cache Serving (Phase 4)**: Optimized poller and collector management for faster dashboard loading.
- **Modernized CI/CD Infrastructure**:
    - Integrated **Ruff** for linting and formatting, **Mypy** for type analysis.
    - Added **pip-audit** for dependency vulnerability scanning (no API key required).
    - Added **Hadolint** for Dockerfile linting and **Frontend Check** for CSS builds.
    - Added **Dependabot** for automated weekly updates (actions, pip, npm).
    - Added **Codecov** integration with coverage thresholds (63% project, 70% patch).
    - Added concurrency groups (cancel stale PR runs) and job timeouts.
    - Added **Makefile** with `install`, `dev`, `test`, `lint`, `format`, `css`, `sidecar`, `clean` targets.
    - Implemented `pyproject.toml` for centralized tool configuration.
    - Reorganized `.gitignore` and `.dockerignore` for better repository hygiene.
- **Test Coverage**: Added 82 new tests (317 total); coverage improved from 65% → 69%.
    - New: `test_token_refresher.py`, `test_builder.py`, `test_utils.py`.
    - Fixed 4 stale patch targets broken by the `oauth_base` refactor.

### Changed
- Refined Dashboard UI with glassmorphism aesthetics and improved interactive feedback.
- Updated `external_metrics` service for better multi-host data aggregation.
- Updated Docker build pipeline to only trigger on versioned tags (`v*`).

### Fixed
- Resolved over 1,200 linting and formatting issues across the codebase.
- Fixed numerous type-related bugs and inconsistencies identified by Mypy.
- Addressed various issues in webhook CRUD API and configuration validation.
- Fixed several edge cases in collector token refreshing and error handling.
