# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.12.1](https://github.com/s3ntin3l8/ai-usage-tracker/compare/v0.12.0...v0.12.1) (2026-04-14)


### Bug Fixes

* **build:** resolve PyInstaller spec paths from repo root via SPECPATH ([9a9a236](https://github.com/s3ntin3l8/ai-usage-tracker/commit/9a9a2367c940b82e31891615f85d54921f460848))

## [0.12.0](https://github.com/s3ntin3l8/ai-usage-tracker/compare/v0.11.1...v0.12.0) (2026-04-14)


### Features

* **5A:** Chart.js token volume charts on History tab (stacked bar + line toggle) ([30a6dc2](https://github.com/s3ntin3l8/ai-usage-tracker/commit/30a6dc2e99bccf1732ee8e4beb58b343e5344210))
* **5B:** add CSV export to /api/v1/usage/history?format=csv ([8f1046e](https://github.com/s3ntin3l8/ai-usage-tracker/commit/8f1046e355274bda8a889c8a4cd72b6632a84cb7))
* **5B:** add WebhookConfig SQLModel table ([45a511d](https://github.com/s3ntin3l8/ai-usage-tracker/commit/45a511d5442a17410e97fefbabfbb8a91275d9e5))
* **5B:** CSV download button in History and Webhook Alerts section in Settings ([672a511](https://github.com/s3ntin3l8/ai-usage-tracker/commit/672a51174ef1cc780c1ffc9e1ff35435350af49a))
* **5B:** webhook breach detection service with Discord and Slack payloads ([36cc907](https://github.com/s3ntin3l8/ai-usage-tracker/commit/36cc90707d6bdebbbc7e4cd4658982587bb20947))
* **5B:** webhook CRUD API, test endpoint, and poller breach detection integration ([054bd11](https://github.com/s3ntin3l8/ai-usage-tracker/commit/054bd11c35e67aec2168a596f9e070086de8ac82))
* **5C:** smart polling sleep mode — 2h interval after 45min of no quota change ([b4b043a](https://github.com/s3ntin3l8/ai-usage-tracker/commit/b4b043a354d440d8195bcd50f42843859827d97f))
* add configurable network binding with security warnings ([8aa8a01](https://github.com/s3ntin3l8/ai-usage-tracker/commit/8aa8a016d3f731c4598b61b825ed7057d671eda5))
* add data retention compaction service (60d hourly, 180d daily) ([90a0323](https://github.com/s3ntin3l8/ai-usage-tracker/commit/90a0323755be98f6227d302903d6312bf57cae0a))
* add Dockerfile and .dockerignore for containerized deployment ([c8db689](https://github.com/s3ntin3l8/ai-usage-tracker/commit/c8db68955c5fcc5ea25995fa9cac46c2e15a1253))
* add JsonFormatter for LOG_FORMAT=json structured logging ([bb4499f](https://github.com/s3ntin3l8/ai-usage-tracker/commit/bb4499f61f0943e266410af4983b641fbac27727))
* add multi-model support for Antigravity IDE tracking ([ad03a0f](https://github.com/s3ntin3l8/ai-usage-tracker/commit/ad03a0f6534367a14563991d1342a588eb00d2a8))
* add sidecar and ingestion improvement ideas to documentation ([3bbaac8](https://github.com/s3ntin3l8/ai-usage-tracker/commit/3bbaac83b558dd23a0e651447f02c0130184e005))
* Add Sidecar Ingest API for external metric tracking ([93632d9](https://github.com/s3ntin3l8/ai-usage-tracker/commit/93632d9c0ea7c34ed78727e12db9a4a2e3e1230f))
* add usage_url links and last updated timestamp to detail modal ([8df811c](https://github.com/s3ntin3l8/ai-usage-tracker/commit/8df811c3dd14dfc133a84d63a222e7afafa7e410))
* align Claude collector with robust gold standard ([feb86cc](https://github.com/s3ntin3l8/ai-usage-tracker/commit/feb86cc0d04dfc7b8862aa5f4949ca8433bfb897))
* align collectors with robust strategies and harden authentication ([5f18223](https://github.com/s3ntin3l8/ai-usage-tracker/commit/5f18223698c576646286338b820a4a64cfb90fdb))
* **auth:** implement aggressive credential autodiscovery for GitHub and Gemini ([ad322c9](https://github.com/s3ntin3l8/ai-usage-tracker/commit/ad322c99829c79cca786389e15a37cd4b19cba3e))
* **backend:** add /api/v1/system/dashboard-layout GET+PUT ([04df5bd](https://github.com/s3ntin3l8/ai-usage-tracker/commit/04df5bdcc188659272848f4e14e500fa07200216))
* **backend:** add dashboard_layout_json column to SystemConfig ([c0b2097](https://github.com/s3ntin3l8/ai-usage-tracker/commit/c0b209725cfccf9af61859aa4d3b582d262ff030))
* **chatgpt:** implement web scraping for account tier and cookie harvesting ([91cc13e](https://github.com/s3ntin3l8/ai-usage-tracker/commit/91cc13e7a5d58f3a38046de993b4c04bcef5214d))
* **ci:** add sidecar-release workflow to build PyInstaller binaries on version tags ([5074c0f](https://github.com/s3ntin3l8/ai-usage-tracker/commit/5074c0f1dc47b6f7657f374940dcc066efb7155a))
* **claude:** add provider identity and keyring support ([0f131da](https://github.com/s3ntin3l8/ai-usage-tracker/commit/0f131da6c62dd507b323aaab2f5197be3033b542))
* **claude:** add Web API fallback via Chrome cookies and enhanced local log parsing ([c701cce](https://github.com/s3ntin3l8/ai-usage-tracker/commit/c701ccebf896a21338e005adddd62eaef935e146))
* **collector:** implement global timeout for concurrent collections ([6996a4a](https://github.com/s3ntin3l8/ai-usage-tracker/commit/6996a4a729ad4098cfbdca759ed54a54015e30b3))
* **collector:** lazy load collectors and move limits to config ([9194187](https://github.com/s3ntin3l8/ai-usage-tracker/commit/9194187e84e122aa76e3d6779a38edf9d93db65e))
* **collectors:** implement consistent API error caching across all collectors ([f1a9730](https://github.com/s3ntin3l8/ai-usage-tracker/commit/f1a97307cf7fe99c7f9d4feebab659dc28060959))
* **collectors:** Implement smart differential fetching to reduce API calls ([ff72bc2](https://github.com/s3ntin3l8/ai-usage-tracker/commit/ff72bc29b2283b346b54aec464220bb0868ec6f7))
* **dashboard-reorder:** drag service rows on the dashboard summary card ([fb4a2f3](https://github.com/s3ntin3l8/ai-usage-tracker/commit/fb4a2f3833e93b3f5021a5440558fcd9e085be52))
* enable OpenCode Go dollar-based usage and GitHub Copilot live tracking ([faa0c0e](https://github.com/s3ntin3l8/ai-usage-tracker/commit/faa0c0e48d6586e479f1b24d9d9fbb5cd4acd8ea))
* enhance Antigravity credits and Claude paid usage tracking ([09ac1d9](https://github.com/s3ntin3l8/ai-usage-tracker/commit/09ac1d9836307a0a8eeecaa27134f21384106037))
* enhance branding and documentation ([e458f44](https://github.com/s3ntin3l8/ai-usage-tracker/commit/e458f448dbc388e13f529e8a1cd120152ec1b9cf))
* enhance quota cards with raw values, data sources, and unlimited state ([6fa3082](https://github.com/s3ntin3l8/ai-usage-tracker/commit/6fa3082c7629068f307dffe7d78713c03a1739c4))
* enhance sidecar with Claude cookie scraping and Flatpak/Snap browser support ([a27594a](https://github.com/s3ntin3l8/ai-usage-tracker/commit/a27594ad420633913a01989653b25d4b2dfe5c4a))
* Feature 5D — native desktop sidecar (macOS menubar + Windows tray) ([1dcc7a3](https://github.com/s3ntin3l8/ai-usage-tracker/commit/1dcc7a30aedc99bbdaeed4d80263561d7a54432c))
* Feature 5D — native desktop sidecar (macOS menubar + Windows tray) ([1dcc7a3](https://github.com/s3ntin3l8/ai-usage-tracker/commit/1dcc7a30aedc99bbdaeed4d80263561d7a54432c))
* finalize AI usage tracker with 11 resilient collectors and documentation ([057e5a0](https://github.com/s3ntin3l8/ai-usage-tracker/commit/057e5a0f6408ddbebfd721b7e35e0c0a29461f7b))
* **frontend:** add code splitting with separate view modules ([3d33df2](https://github.com/s3ntin3l8/ai-usage-tracker/commit/3d33df2cf8dba8fac4d959be11b7bb54a8aa28e2))
* **frontend:** Add comprehensive JSDoc annotations for type safety ([a235cf0](https://github.com/s3ntin3l8/ai-usage-tracker/commit/a235cf0e8c1675d68e7d22cb7c75fcc26a02a278))
* **frontend:** add editMode + layout cache to STATE ([b986cd9](https://github.com/s3ntin3l8/ai-usage-tracker/commit/b986cd96d517a71298164478bcddfc8e8190e544))
* **frontend:** add getDashboardLayout + putDashboardLayout ([46abe8f](https://github.com/s3ntin3l8/ai-usage-tracker/commit/46abe8f0acafcdb246c853b0072e9d0cb30258a6))
* **frontend:** add layout helpers for dashboard reorder ([ef2411b](https://github.com/s3ntin3l8/ai-usage-tracker/commit/ef2411b36fa497f639133ba88b04d41fcd204167))
* **frontend:** add Sortable.js lazy CDN loader ([33ab94a](https://github.com/s3ntin3l8/ai-usage-tracker/commit/33ab94a2a184810b766a984b43f7813f476d6d8e))
* **frontend:** auto-refresh, bright mode, pace icons, layout improvements ([8ba4bc8](https://github.com/s3ntin3l8/ai-usage-tracker/commit/8ba4bc890f8367c474e937c9e67804ae7ceb2e15))
* **frontend:** edit-mode toggle + drag-to-reorder via Sortable.js ([35f0414](https://github.com/s3ntin3l8/ai-usage-tracker/commit/35f041487239b849fc623d50404d0605df15276f))
* **frontend:** extract dashboard into separate lazy-loaded module ([7593478](https://github.com/s3ntin3l8/ai-usage-tracker/commit/7593478c20279787e0ca8b879d26c8d940a32ad9))
* **frontend:** extract Fleet view into separate module ([4fbd5a8](https://github.com/s3ntin3l8/ai-usage-tracker/commit/4fbd5a865e3163c6020a403172c81cbfde97ebcd))
* **frontend:** persist active tab via URL hash ([b3eee58](https://github.com/s3ntin3l8/ai-usage-tracker/commit/b3eee58ec74c19cb80c9f598261318b1b220bf47))
* **frontend:** persist ui state and toggle visibility for cards ([a75717c](https://github.com/s3ntin3l8/ai-usage-tracker/commit/a75717c15907a9db423a20ccda8dba981b5938e9))
* **frontend:** render dashboard with saved layout applied ([07e6159](https://github.com/s3ntin3l8/ai-usage-tracker/commit/07e6159a74963664d6c565d2502f8743897c154f))
* **frontend:** stamp data-provider-id/data-card-key + grip glyph ([cf7a6fa](https://github.com/s3ntin3l8/ai-usage-tracker/commit/cf7a6fa9f9b39db4217dcd037159f8a1cdea0faf))
* **frontend:** unify header controls as icon-only buttons ([8235435](https://github.com/s3ntin3l8/ai-usage-tracker/commit/82354356e57b2969542a569c6c94fc718646076b))
* **gemini:** consolidate model quota cards into families ([cd75dd2](https://github.com/s3ntin3l8/ai-usage-tracker/commit/cd75dd2e79a2dfe068ec3171efd647e30fa15afe))
* **history:** adapt bucket granularity to selected time window ([d53365a](https://github.com/s3ntin3l8/ai-usage-tracker/commit/d53365a45a4c4119fcc48cb09125fad0252c857e))
* implement Antigravity IDE 9th service with real-time telemetry from quota.json ([e5e6321](https://github.com/s3ntin3l8/ai-usage-tracker/commit/e5e6321d61d613d5f50b94b87a979789eb94bdb9))
* implement ChatGPT Codex local log parser for remaining capacity ([d1b0927](https://github.com/s3ntin3l8/ai-usage-tracker/commit/d1b09277464ac02a9b4acbd4701b2381d8cdea39))
* implement Claude OAuth token refresh with persistence ([a23d612](https://github.com/s3ntin3l8/ai-usage-tracker/commit/a23d612cff167dabb58cafc3f624ea9c602fee12))
* implement cross-platform path support and improve chrome cookie extraction ([ed166b7](https://github.com/s3ntin3l8/ai-usage-tracker/commit/ed166b71aa0e8010a7192419c0dd948557af03be))
* implement GitHub Copilot API integration with subscription fallback ([578799a](https://github.com/s3ntin3l8/ai-usage-tracker/commit/578799a699eb21cbbffc3b7439f1c960a934980e))
* implement GitHub OAuth Device Flow and multi-browser cookie support cleanup ([06902d2](https://github.com/s3ntin3l8/ai-usage-tracker/commit/06902d25d3334c624be583ea68424d5bac462ddc))
* implement multi-browser cookie support (Chrome, Edge, Firefox, Safari) and enhance sidecar extraction ([79e9f1d](https://github.com/s3ntin3l8/ai-usage-tracker/commit/79e9f1d668a7101b22137519b05269dd9f0ba4a6))
* implement sidecar ingestion API and listener pattern ([c522934](https://github.com/s3ntin3l8/ai-usage-tracker/commit/c522934519c628064de3dc7f90d391cc50678768))
* implement UI drill-down for detailed quota metrics ([6c26b86](https://github.com/s3ntin3l8/ai-usage-tracker/commit/6c26b86ac490d967ba0ee4087670405ec4f13296))
* initial local-first AI runway dashboard with 8 collectors ([daee28b](https://github.com/s3ntin3l8/ai-usage-tracker/commit/daee28bf7fe03c427c061e7ecd4f73981716ea93))
* integrate internal monitoring improvements and harden collectors ([92b64c2](https://github.com/s3ntin3l8/ai-usage-tracker/commit/92b64c2196ee27853a5e50f7065b317337f4c1b4))
* keychain consolidation, OAuth auto-discovery, and codebase hardening ([b56ddb5](https://github.com/s3ntin3l8/ai-usage-tracker/commit/b56ddb5a37d9176467728001a9ed9c1c2416481d))
* migrate Settings to pydantic_settings.BaseSettings with LOG_FORMAT field ([50da05c](https://github.com/s3ntin3l8/ai-usage-tracker/commit/50da05c28c07c99d1c8f135af32e2bf8e66aa305))
* **opencode:** add multi-window limit cards (5h, week, month) ([5d83c67](https://github.com/s3ntin3l8/ai-usage-tracker/commit/5d83c67b9622971881f0ff9650d3fd605b1ae0ea))
* **opencode:** add sidecar aggregation for multi-host usage ([b0e7f3d](https://github.com/s3ntin3l8/ai-usage-tracker/commit/b0e7f3d534829b8636d9b489977a9dcc746af2a4))
* **opencode:** implement web API collector with Chrome cookie support ([b18b1d1](https://github.com/s3ntin3l8/ai-usage-tracker/commit/b18b1d17e235c06d92bbd1b0e0d57d6921b25351))
* replace Tailwind CDN with production build setup ([00326f6](https://github.com/s3ntin3l8/ai-usage-tracker/commit/00326f687dc9c56067a02b6d0813e98c315824bd))
* setup CI/CD workflow and enhance test structure for GHCR ([e3eab6d](https://github.com/s3ntin3l8/ai-usage-tracker/commit/e3eab6d69ae1e2d61b5848f7abcdc276ab75bec7))
* **sidecar-app:** add autostart login-item install for macOS and Windows ([89dae97](https://github.com/s3ntin3l8/ai-usage-tracker/commit/89dae974b14733a180c3fd2999ff0ecaf235d322))
* **sidecar-app:** add GitHub Releases update checker ([52b49d7](https://github.com/s3ntin3l8/ai-usage-tracker/commit/52b49d77d34ccbefe40d426eafb498d54cf3a05d))
* **sidecar-app:** add PyInstaller spec files for macOS and Windows ([94fe008](https://github.com/s3ntin3l8/ai-usage-tracker/commit/94fe0084c1b5dc32e3b185372ef917b53094caf3))
* **sidecar-app:** add sidecar_app package skeleton with tray, daemon, and config modules ([8668210](https://github.com/s3ntin3l8/ai-usage-tracker/commit/866821052b38b360f3353526956987fe9dfd77ca))
* **sidecar-app:** wire first-run notification and menu action callbacks ([17a14a2](https://github.com/s3ntin3l8/ai-usage-tracker/commit/17a14a20d107c98f3190b380859fef5246c5872b))
* split zai/kimi into API and Plan/Coding variants ([6692bb4](https://github.com/s3ntin3l8/ai-usage-tracker/commit/6692bb4122365e6b1b87943c96c6626e48e13c2f))
* trigger daily snapshot compaction in background poller ([a444d10](https://github.com/s3ntin3l8/ai-usage-tracker/commit/a444d10cc53091d9fa94418b40b89452c21edb13))
* **ui:** add buildProviderSummaryCard component ([52e028c](https://github.com/s3ntin3l8/ai-usage-tracker/commit/52e028c3f40a8446b7505cc56a0c7fab67582fd6))
* **ui:** add health overview bar to dashboard ([00ac8e3](https://github.com/s3ntin3l8/ai-usage-tracker/commit/00ac8e3f0bcb3e4afb8c9a238741511fdf5b41a2))
* **ui:** add provider drill-down modal with sparklines ([691139c](https://github.com/s3ntin3l8/ai-usage-tracker/commit/691139c712d28d08c577a8f1e3d41155d5830889))
* **ui:** add provider sparkline strip to history tab ([792265d](https://github.com/s3ntin3l8/ai-usage-tracker/commit/792265de19bb0bec3eb6ba03a807a2b4084caf3c))
* **ui:** add time range, metric switcher, and CSV filter to history tab ([50affd5](https://github.com/s3ntin3l8/ai-usage-tracker/commit/50affd58777756e2862ed8f4bb79564835bbf275))
* **ui:** add Window filter dimension to dashboard filter bar ([97ea70f](https://github.com/s3ntin3l8/ai-usage-tracker/commit/97ea70f8c98d406d01209360541f6e5b5d5dcd11))
* **ui:** wire provider summary cards into dashboard grid ([09ac40f](https://github.com/s3ntin3l8/ai-usage-tracker/commit/09ac40f221c089f1b6dc20a25cf5ca297790fcb6))
* update zAI collector to use verified monitor endpoint ([d052f51](https://github.com/s3ntin3l8/ai-usage-tracker/commit/d052f511d81ffab743f0d9a72ae69de2faefab25))
* upgrade Gemini collector with OAuth API support and sidecar integration ([1482190](https://github.com/s3ntin3l8/ai-usage-tracker/commit/1482190752d304f55177f7613b60948ffe06e1b5))


### Bug Fixes

* **5B:** address code review issues in webhook CRUD API ([ece8b84](https://github.com/s3ntin3l8/ai-usage-tracker/commit/ece8b84fb8e14abc93355015c296aaec187ee65c))
* **5B:** address code review issues in WebhookConfig and webhooks service ([5c25b43](https://github.com/s3ntin3l8/ai-usage-tracker/commit/5c25b43482246c8b66a02ad51032d74c4cb65238))
* address post-redesign UI and backend issues ([ae0bd82](https://github.com/s3ntin3l8/ai-usage-tracker/commit/ae0bd822bdb9d258dbc3c6365602ada4bb288e9c))
* **anthropic:** offload blocking file I/O in _get_claude_local_enhanced to asyncio.to_thread ([00d7780](https://github.com/s3ntin3l8/ai-usage-tracker/commit/00d77808a4dca5e6687ba80b69be5a5c7ed180f8))
* **backend:** prevent duplicate cards when default + dynamic collectors coexist for same provider ([ccde053](https://github.com/s3ntin3l8/ai-usage-tracker/commit/ccde053d58a8c6fe9f85b573200c37fc515264c6))
* **build:** declare setuptools package discovery in pyproject.toml ([9af08fc](https://github.com/s3ntin3l8/ai-usage-tracker/commit/9af08fc1995bc3cce24b3a98c8ba9b8b1e66081a))
* **chrome_cookies:** use AES-128-CBC instead of AES-GCM for v10/v11 cookie decryption on macOS and Linux ([0440c6e](https://github.com/s3ntin3l8/ai-usage-tracker/commit/0440c6eccf731eb2f6f1b2bb789ec958f39db619))
* **ci:** move Docker + sidecar builds into release-please.yml ([8c88844](https://github.com/s3ntin3l8/ai-usage-tracker/commit/8c8884463895de136806fcc0f7a7eddb8dd9b02f))
* **ci:** trigger build-and-push on release published event ([9e579db](https://github.com/s3ntin3l8/ai-usage-tracker/commit/9e579db8cf8542e3f5f52ea737cc8dfef06f33d4))
* **collector:** fix Kimi and MiniMax providers with new features ([a747c80](https://github.com/s3ntin3l8/ai-usage-tracker/commit/a747c80db1278435469a36e02660b0336d77bd74))
* **collector:** improve Anthropic and related collectors ([9ad1782](https://github.com/s3ntin3l8/ai-usage-tracker/commit/9ad1782c035dcb804c0416f7066bde1f9f75549a))
* **collector:** improve GitHub Copilot collector ([8200bcf](https://github.com/s3ntin3l8/ai-usage-tracker/commit/8200bcf194c8cadcf34af18526759bb262aaa521))
* **collector:** improve Ollama and other collectors ([5cb30fd](https://github.com/s3ntin3l8/ai-usage-tracker/commit/5cb30fd7f73c738d9bd636e603b939321246fdc9))
* **collector:** improve OpenRouter and OpenCode collectors ([a748c1c](https://github.com/s3ntin3l8/ai-usage-tracker/commit/a748c1c62aa6412683097ac751e53ba63baad523))
* **collector:** merge zAI collectors into single provider ([3c48eb7](https://github.com/s3ntin3l8/ai-usage-tracker/commit/3c48eb7279b129249c5e7351c3e68ef531f87587))
* comprehensive bug fixes and test improvements ([7c7ebb4](https://github.com/s3ntin3l8/ai-usage-tracker/commit/7c7ebb4070897307acc271ce8e019d7e2b553a9e))
* consistent timezone handling and OpenCode rolling window display ([b78020c](https://github.com/s3ntin3l8/ai-usage-tracker/commit/b78020c95ec3edae33c3cbf123210d1e2a8975ec))
* **core:** improve backend services and configuration ([ccf4630](https://github.com/s3ntin3l8/ai-usage-tracker/commit/ccf46302ac694117225f7a08f2bc0ed044d9a0ea))
* **dashboard-reorder:** allow modal open in edit mode, subtle smaller grip ([9e46dd8](https://github.com/s3ntin3l8/ai-usage-tracker/commit/9e46dd8d9a0e7bb387b02542ada1e927761fba58))
* **dashboard-reorder:** bright-mode styles, exit on tab switch, whole-card drag, subitem reorder ([5d36fbe](https://github.com/s3ntin3l8/ai-usage-tracker/commit/5d36fbea6b3cc5c6e87c16082d97d7741a4c1a1c))
* font sizes, polling, filter default, GitHub identity, bright mode ([6111da7](https://github.com/s3ntin3l8/ai-usage-tracker/commit/6111da76184acbceb40942d1fec7d059bdd6fd23))
* **frontend:** correctly calculate remaining value in card subtitle ([7d0d808](https://github.com/s3ntin3l8/ai-usage-tracker/commit/7d0d808ce68ff5cdd4bc64452c0be031e814dd53))
* **frontend:** define isDisabled in buildModalContent to fix ReferenceError ([6b6c8d8](https://github.com/s3ntin3l8/ai-usage-tracker/commit/6b6c8d867cc67a5509c1ee08d6e2675e37eaf162))
* **frontend:** don't re-sort provider modal items after applyOrder ([084e76f](https://github.com/s3ntin3l8/ai-usage-tracker/commit/084e76ff3f265623cc0708efcff2fe171184c0f0))
* **frontend:** improve settings UI and components ([ab12f07](https://github.com/s3ntin3l8/ai-usage-tracker/commit/ab12f071b1906adaa0f374b950dca6c4e4b900c1))
* **frontend:** remove purple square from provider icons ([7060779](https://github.com/s3ntin3l8/ai-usage-tracker/commit/7060779e6b9ef85ce79aafccdd673bc2e960d0b9))
* **frontend:** remove TZ from modal, fix tier badge truncation, widen status line ([95abdeb](https://github.com/s3ntin3l8/ai-usage-tracker/commit/95abdeb6ad0b783aab2d30a11486b8b43331585c))
* **gemini:** show all model quotas including gemini-3 with project discovery ([47e0c62](https://github.com/s3ntin3l8/ai-usage-tracker/commit/47e0c6225aeb3c8f5f546399de7cc1db2d259599))
* **github:** extract quotas from user response for free tier ([06ab2cc](https://github.com/s3ntin3l8/ai-usage-tracker/commit/06ab2ccf62f195f0b38524614bcb1eed4d0764ff))
* history fractional days, Anthropic windows/tier, readability, modal ([4139414](https://github.com/s3ntin3l8/ai-usage-tracker/commit/413941417b7745672690f90e5c5e793fc1c3b53e))
* **history:** prevent LIMIT from hiding older days in history tab ([349b321](https://github.com/s3ntin3l8/ai-usage-tracker/commit/349b3218d4a7ffa4ee22348bbf6915586216b67f))
* implement critical error handling and security improvements ([75360b5](https://github.com/s3ntin3l8/ai-usage-tracker/commit/75360b5e8defac6266f6e4628447ae81dcb114b8))
* **ingest:** always write back redacted detail when oauth_token present without refresh_token ([e491e8e](https://github.com/s3ntin3l8/ai-usage-tracker/commit/e491e8e365fae2e57757688651f2bd18d30603ff))
* **ingest:** return 503 when INGEST_API_KEY is empty to prevent insecure HMAC acceptance ([6ab8045](https://github.com/s3ntin3l8/ai-usage-tracker/commit/6ab80451ef8d1a61a3e47db79e98f0ccb3c8ff38))
* **lint:** apply ruff format to all files and fix noqa placement ([350108d](https://github.com/s3ntin3l8/ai-usage-tracker/commit/350108dee05ec0cb873b7c0c694cdb4d8c9db8a3))
* **lint:** resolve all ruff warnings across codebase ([672603e](https://github.com/s3ntin3l8/ai-usage-tracker/commit/672603e262d8b5b8d504f891ab49a4bd72191828))
* **lint:** resolve mypy errors blocking CI ([3455a6a](https://github.com/s3ntin3l8/ai-usage-tracker/commit/3455a6ac23bf51a57acb5ca94560d72d625a1f3b))
* move Gemini OAuth credentials to environment variables in scripts ([8cc90ad](https://github.com/s3ntin3l8/ai-usage-tracker/commit/8cc90ad1883159ff6d396ef7e8b603260ef49d0b))
* **opencode:** handle deprecated API endpoint gracefully ([9fef7b0](https://github.com/s3ntin3l8/ai-usage-tracker/commit/9fef7b08bfb9249e52e042aad223abd33d7afd3d))
* remove deleted tailwind.config.js and excluded scripts/ from Dockerfile ([3ad6a75](https://github.com/s3ntin3l8/ai-usage-tracker/commit/3ad6a75a78676e40100536c8093c85384cc8a5b8))
* remove stale REFRESH_CONFIG and setChartView imports that broke app module load ([e5fa6b6](https://github.com/s3ntin3l8/ai-usage-tracker/commit/e5fa6b6acab1a06dcc4ad4c59a45a8eef44dc014))
* resolve CI failures from Dependabot PRs ([02f8372](https://github.com/s3ntin3l8/ai-usage-tracker/commit/02f8372720ec0133a4bf1ee0526b1c001dab2cc2))
* resolve crashes, security vulnerabilities, and sidecar integration issues ([1a4287d](https://github.com/s3ntin3l8/ai-usage-tracker/commit/1a4287d809b9e79e33e4cda76f3a586652896bd5))
* resolve provider token handoff bug in ingestion process ([bda000a](https://github.com/s3ntin3l8/ai-usage-tracker/commit/bda000a61d9bad3e7656528b7e8dbba128ee039f))
* **security:** reject default INGEST_API_KEY at runtime, require explicit configuration ([b69b6a0](https://github.com/s3ntin3l8/ai-usage-tracker/commit/b69b6a0d8733d27fcf8e761f44b8e020770a9687))
* **server:** always read index.html from disk, remove in-memory HTML cache ([8195767](https://github.com/s3ntin3l8/ai-usage-tracker/commit/8195767bc2ee3bf827fb3390fee37235dcb164c1))
* **sidecar-app:** remove invalid onefile=True from windows.spec EXE ([c27daab](https://github.com/s3ntin3l8/ai-usage-tracker/commit/c27daab24f54ba06dc8b07bc1d1a610ec619d8ec))
* **sidecar-app:** silence OSError in _windows_install for locked-down registry ([352e329](https://github.com/s3ntin3l8/ai-usage-tracker/commit/352e3295dde32a19836023f5a1bab7a58d802197))
* **sidecar-app:** use sys._MEIPASS for frozen PyInstaller path resolution ([6f06680](https://github.com/s3ntin3l8/ai-usage-tracker/commit/6f066803426a3dee3ae6081005c8757559159f08))
* **sidecar-app:** use xdg-open from PATH instead of hardcoded /usr/bin ([52db149](https://github.com/s3ntin3l8/ai-usage-tracker/commit/52db1497c1fd04000ca5e64af40f6c1bd071f3da))
* **sidecar:** fix remaining _windows_cred_cache = None assignments in shutdown and daemon paths ([49bf558](https://github.com/s3ntin3l8/ai-usage-tracker/commit/49bf55874e0860f5d00a29f2c5fde4b5f95fce0c))
* **sidecar:** initialize _windows_cred_cache as empty dict instead of None to prevent TypeError on write ([e16beba](https://github.com/s3ntin3l8/ai-usage-tracker/commit/e16bebabca4a16e4e9aa4d3c81ac3c2a8fae214b))
* **sidecar:** set default max_size_mb=10 in queue_rotate to prevent TypeError crash when called without args ([06e75dd](https://github.com/s3ntin3l8/ai-usage-tracker/commit/06e75dd7ae654fcfa5cecd33f4b72e401cc41a9d))
* **tests:** remove unused imports and sort import block in test_sidecar_autostart ([fa1f9f2](https://github.com/s3ntin3l8/ai-usage-tracker/commit/fa1f9f20041cadd6beb7480356348a24a412148a))
* **tests:** stub GEMINI_OAUTH_CLIENT_ID/SECRET in refresher fallback test ([74a744d](https://github.com/s3ntin3l8/ai-usage-tracker/commit/74a744db77b28e9ffa60ec945eb847fc28806bf2))
* **tests:** update collector tests ([65b7e22](https://github.com/s3ntin3l8/ai-usage-tracker/commit/65b7e2295a62bb750aeed16479cb7ed5cc9c749b))
* **test:** update registry count assertion to 13 (added kimi_k2) ([635d971](https://github.com/s3ntin3l8/ai-usage-tracker/commit/635d9714ecd840b4a84bbf45051cb63871e508af))
* **test:** use tuple for RECOGNIZED_COOKIE_NAMES (frozenset is unordered) ([06ef093](https://github.com/s3ntin3l8/ai-usage-tracker/commit/06ef0931bfcc8161c8dda39f45e234ed1e510428))
* **token-refresh:** add settings fallback for client_id/secret and ChatGPT support ([993a4be](https://github.com/s3ntin3l8/ai-usage-tracker/commit/993a4bee19ba9561cb3ca8e227fc9de858047fb0))
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
* **sidecar:** add performance optimizations for daemon mode ([ec27124](https://github.com/s3ntin3l8/ai-usage-tracker/commit/ec271246023b3d7ce850aa3a4b19134a379cb87c))

## [0.11.1](https://github.com/s3ntin3l8/ai-usage-tracker/compare/v0.11.0...v0.11.1) (2026-04-14)


### Bug Fixes

* **tests:** stub GEMINI_OAUTH_CLIENT_ID/SECRET in refresher fallback test ([74a744d](https://github.com/s3ntin3l8/ai-usage-tracker/commit/74a744db77b28e9ffa60ec945eb847fc28806bf2))

## [0.11.0](https://github.com/s3ntin3l8/ai-usage-tracker/compare/v0.10.1...v0.11.0) (2026-04-14)


### Features

* **backend:** add /api/v1/system/dashboard-layout GET+PUT ([04df5bd](https://github.com/s3ntin3l8/ai-usage-tracker/commit/04df5bdcc188659272848f4e14e500fa07200216))
* **backend:** add dashboard_layout_json column to SystemConfig ([c0b2097](https://github.com/s3ntin3l8/ai-usage-tracker/commit/c0b209725cfccf9af61859aa4d3b582d262ff030))
* **dashboard-reorder:** drag service rows on the dashboard summary card ([fb4a2f3](https://github.com/s3ntin3l8/ai-usage-tracker/commit/fb4a2f3833e93b3f5021a5440558fcd9e085be52))
* **frontend:** add editMode + layout cache to STATE ([b986cd9](https://github.com/s3ntin3l8/ai-usage-tracker/commit/b986cd96d517a71298164478bcddfc8e8190e544))
* **frontend:** add getDashboardLayout + putDashboardLayout ([46abe8f](https://github.com/s3ntin3l8/ai-usage-tracker/commit/46abe8f0acafcdb246c853b0072e9d0cb30258a6))
* **frontend:** add layout helpers for dashboard reorder ([ef2411b](https://github.com/s3ntin3l8/ai-usage-tracker/commit/ef2411b36fa497f639133ba88b04d41fcd204167))
* **frontend:** add Sortable.js lazy CDN loader ([33ab94a](https://github.com/s3ntin3l8/ai-usage-tracker/commit/33ab94a2a184810b766a984b43f7813f476d6d8e))
* **frontend:** edit-mode toggle + drag-to-reorder via Sortable.js ([35f0414](https://github.com/s3ntin3l8/ai-usage-tracker/commit/35f041487239b849fc623d50404d0605df15276f))
* **frontend:** persist active tab via URL hash ([b3eee58](https://github.com/s3ntin3l8/ai-usage-tracker/commit/b3eee58ec74c19cb80c9f598261318b1b220bf47))
* **frontend:** render dashboard with saved layout applied ([07e6159](https://github.com/s3ntin3l8/ai-usage-tracker/commit/07e6159a74963664d6c565d2502f8743897c154f))
* **frontend:** stamp data-provider-id/data-card-key + grip glyph ([cf7a6fa](https://github.com/s3ntin3l8/ai-usage-tracker/commit/cf7a6fa9f9b39db4217dcd037159f8a1cdea0faf))
* **frontend:** unify header controls as icon-only buttons ([8235435](https://github.com/s3ntin3l8/ai-usage-tracker/commit/82354356e57b2969542a569c6c94fc718646076b))
* **history:** adapt bucket granularity to selected time window ([d53365a](https://github.com/s3ntin3l8/ai-usage-tracker/commit/d53365a45a4c4119fcc48cb09125fad0252c857e))


### Bug Fixes

* **backend:** prevent duplicate cards when default + dynamic collectors coexist for same provider ([ccde053](https://github.com/s3ntin3l8/ai-usage-tracker/commit/ccde053d58a8c6fe9f85b573200c37fc515264c6))
* **ci:** move Docker + sidecar builds into release-please.yml ([8c88844](https://github.com/s3ntin3l8/ai-usage-tracker/commit/8c8884463895de136806fcc0f7a7eddb8dd9b02f))
* **dashboard-reorder:** allow modal open in edit mode, subtle smaller grip ([9e46dd8](https://github.com/s3ntin3l8/ai-usage-tracker/commit/9e46dd8d9a0e7bb387b02542ada1e927761fba58))
* **dashboard-reorder:** bright-mode styles, exit on tab switch, whole-card drag, subitem reorder ([5d36fbe](https://github.com/s3ntin3l8/ai-usage-tracker/commit/5d36fbea6b3cc5c6e87c16082d97d7741a4c1a1c))
* **frontend:** don't re-sort provider modal items after applyOrder ([084e76f](https://github.com/s3ntin3l8/ai-usage-tracker/commit/084e76ff3f265623cc0708efcff2fe171184c0f0))
* **frontend:** remove purple square from provider icons ([7060779](https://github.com/s3ntin3l8/ai-usage-tracker/commit/7060779e6b9ef85ce79aafccdd673bc2e960d0b9))
* **frontend:** remove TZ from modal, fix tier badge truncation, widen status line ([95abdeb](https://github.com/s3ntin3l8/ai-usage-tracker/commit/95abdeb6ad0b783aab2d30a11486b8b43331585c))
* **history:** prevent LIMIT from hiding older days in history tab ([349b321](https://github.com/s3ntin3l8/ai-usage-tracker/commit/349b3218d4a7ffa4ee22348bbf6915586216b67f))
* **token-refresh:** add settings fallback for client_id/secret and ChatGPT support ([993a4be](https://github.com/s3ntin3l8/ai-usage-tracker/commit/993a4bee19ba9561cb3ca8e227fc9de858047fb0))

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
