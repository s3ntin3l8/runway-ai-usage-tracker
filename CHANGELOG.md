# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.10.3](https://github.com/s3ntin3l8/runway-ai-usage-tracker/compare/v2.10.2...v2.10.3) (2026-07-19)


### Bug Fixes

* **webapp:** stop masking expired upstream SSO sessions as a backend outage ([#206](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/206)) ([020c2cf](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/020c2cfd7d1e508909f804005cd911563f484ea2))

## [2.10.2](https://github.com/s3ntin3l8/runway-ai-usage-tracker/compare/v2.10.1...v2.10.2) (2026-07-19)


### Bug Fixes

* **perf:** eliminate 2s dashboard load and cross-provider data leak ([#204](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/204)) ([c817e46](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/c817e463c972b85c55c71ce39b8790eda7982069))

## [2.10.1](https://github.com/s3ntin3l8/runway-ai-usage-tracker/compare/v2.10.0...v2.10.1) (2026-07-19)


### Bug Fixes

* **home:** decouple summary cards from the slow cumulative query ([#201](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/201)) ([2710e06](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/2710e066ef850f6efd8414cdbeb3de73fae7c654))
* resolve CodeQL duplicate-import alert + forward-auth test isolation ([#198](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/198)) ([d919869](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/d919869b8ce53f5e1da291ed5d03cae136888e2b))
* **sidecar:** preserve exec bit through macOS self-update swap ([#203](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/203)) ([934c250](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/934c25074e62ab9849c129f23441e2f2ba2414e3))
* **webapp:** auto-recover BootGate from transient backend outages ([#200](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/200)) ([8217fb9](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/8217fb9ee735017a30ed72fe88c334e17061e85c))

## [2.10.0](https://github.com/s3ntin3l8/runway-ai-usage-tracker/compare/v2.9.0...v2.10.0) (2026-07-16)


### Features

* **auth:** support Authentik/forward-auth headers with optional group allowlist ([#191](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/191)) ([4b5765d](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/4b5765d5edf0d689f9e883e5733ae184558e7502))
* **webapp:** add pull-to-refresh gesture ([#194](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/194)) ([994751f](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/994751fca5e4058899c0e079a93f5ac2471cf0d5))


### Bug Fixes

* gate Claude workflow on trusted commenters, enable real PR reviews ([#192](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/192)) ([9e32274](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/9e32274818f04e6a6e9a4ae0bcc478ed176bffd6))

## [2.9.0](https://github.com/s3ntin3l8/runway-ai-usage-tracker/compare/v2.8.1...v2.9.0) (2026-07-14)


### Features

* **pwa:** add Apple launch/splash screens for iOS home-screen install ([#183](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/183)) ([79cbea7](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/79cbea71d3771ed99de3a21fb85ad72421fa53e2))
* **ui:** add shimmer sweep animation to Skeleton ([#181](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/181)) ([60e3f7a](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/60e3f7aaff74de4ff1a3054bad23015704fac4f8))


### Bug Fixes

* **opencode-free:** all-time token total now respects exclude-cache toggle ([#185](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/185)) ([96afd92](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/96afd92cf56f40b1a96d977e99c143d81b8a3090))
* **opencode:** stop lumping BYOK/OpenRouter/Ollama usage into the Go tier ([#188](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/188)) ([ff64a65](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/ff64a6510a7599cbf4b94e60c6e7ddff305942d7)), closes [#182](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/182)

## [2.8.1](https://github.com/s3ntin3l8/runway-ai-usage-tracker/compare/v2.8.0...v2.8.1) (2026-07-12)


### Bug Fixes

* stop open quota windows from hiding all closed history of the same type ([#177](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/177)) ([0303d4c](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/0303d4cf6d817064fe1bb59bb8bd505952b5c2db))

## [2.8.0](https://github.com/s3ntin3l8/runway-ai-usage-tracker/compare/v2.7.2...v2.8.0) (2026-07-12)


### Features

* enable strict-checks for detect-secrets in test-frontend ([#173](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/173)) ([d64f9cc](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/d64f9ccdac8c9ca5514f79ecb1819d12ded1bcb2))


### Bug Fixes

* sharpen 90d history chart resolution and fill open quota-window totals ([#176](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/176)) ([edabfc5](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/edabfc5611e061c4222da0b27e78e54dd2acb903))
* sidecar runtime resilience, fleet update staleness, chatgpt identity mismatch ([#175](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/175)) ([3d7d360](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/3d7d36077a66f687320e09769c3be2b96d58470a))

## [2.7.2](https://github.com/s3ntin3l8/runway-ai-usage-tracker/compare/v2.7.1...v2.7.2) (2026-07-05)


### Bug Fixes

* stop an expired Antigravity token from clobbering a valid cached one ([#170](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/170)) ([6b8646e](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/6b8646edadc770d245a558b28d1920ae78eb1933))

## [2.7.1](https://github.com/s3ntin3l8/runway-ai-usage-tracker/compare/v2.7.0...v2.7.1) (2026-07-02)


### Bug Fixes

* drop retired Anthropic Sonnet Weekly quota window ([#169](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/169)) ([72c731e](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/72c731e94b7967a7c50c9b61232a068bbeb0fc78))
* reconcile Insights token/cost tiles with lifetime totals ([#167](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/167)) ([1a9bfa5](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/1a9bfa57f2af55f1ab5a90c123271826bf761450))

## [2.7.0](https://github.com/s3ntin3l8/runway-ai-usage-tracker/compare/v2.6.2...v2.7.0) (2026-06-29)


### Features

* consolidate session project labels to the repo root ([#164](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/164)) ([e3b4212](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/e3b4212315c5f0147eae258aa4bf04bd05c32665))


### Bug Fixes

* antigravity empty forecast chart + opencode duplicate "rolling free" window ([c3b7219](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/c3b7219b8d796e09d1a3bf7f5de7d887bf1e22c3))
* **forecast:** match forecast by full card identity, not window_type alone ([8ed6fbf](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/8ed6fbffbdf8daf413a0293f978895e6590117f9))
* **opencode:** stop emitting duplicate "rolling free" window card ([09d23ed](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/09d23ed57598c6fa636a7d0be7f2ffbe8947aad9))
* stop reset_at jitter from flooding usage_windows ([#165](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/165)) ([f2e6bd7](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/f2e6bd7e4aba61f63fb3aa7a47c7f9790f460342))

## [2.6.2](https://github.com/s3ntin3l8/runway-ai-usage-tracker/compare/v2.6.1...v2.6.2) (2026-06-19)


### Bug Fixes

* **antigravity:** label cards with the email instead of "Default" ([#153](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/153)) ([9466c19](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/9466c19748b806ad31e2e6523a4c38809346e291))

## [2.6.1](https://github.com/s3ntin3l8/runway-ai-usage-tracker/compare/v2.6.0...v2.6.1) (2026-06-19)


### Bug Fixes

* add "expiry_date" to the allowed key tuple in fleet.py (one line). ([3196945](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/319694558ec2cfab12281a6d07bc41431063790e))
* **antigravity:** resolve token identity mismatch that blanked quota ([#152](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/152)) ([f9b33af](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/f9b33af76ac6c0e7cc176e88b4aa4b1bcc985963))
* **token-cache:** restore cookie lookup for durable-identity collectors + pass expiry_date through ingest ([3196945](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/319694558ec2cfab12281a6d07bc41431063790e))

## [2.6.0](https://github.com/s3ntin3l8/runway-ai-usage-tracker/compare/v2.5.0...v2.6.0) (2026-06-19)


### Features

* **antigravity:** cloud API quota collector + sidecar event extractor ([#146](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/146)) ([0565080](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/05650809cc68966a91fc56f23f640a12152ec4ce))
* **antigravity:** resolve email via userinfo + cleanup stale LSP-era data ([#148](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/148)) ([dda572e](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/dda572e4b57ca2b6261ef9a8e35278a5511b63d0))
* **dashboard:** polymorphic card hero for token-tracking and pay-by-use providers ([#138](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/138)) ([a9dd18c](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/a9dd18cbfe90d0d2881e2f7c999dd5c064ed4787))
* **detail:** polymorphic Overview/Forecast/KPIs for token & spend providers ([#140](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/140)) ([e0244dd](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/e0244ddd07cd672bcc0b7dce9a95834e5c62e0fe))
* **fleet:** add sidecar-update badge to the nav rail ([#147](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/147)) ([7de2b9a](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/7de2b9a9951196fd77c8c405878501bff7a13cc0))
* **github:** resolve stable account_id from login/email instead of "default" ([#144](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/144)) ([baf49fa](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/baf49fa9dd42cdb0fd7f6777f607d4fedd559a0b))


### Bug Fixes

* **antigravity:** collapse 11 quota cards into 4 stable pool windows ([c39d68d](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/c39d68da1926b7ca8d8a0ff1db39b6c0a57e7325))
* **antigravity:** collapse 11 quota cards into 4 stable pool windows ([c68d0fa](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/c68d0faad84b41b6ee61dba78964f57b0129fac1))
* **config:** move inline .env.example comments to own lines (Docker Compose token poisoning) ([#143](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/143)) ([ed58e4c](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/ed58e4c43e0d9eb627c34fca0f659cfff2a628de))
* **debug:** update stale DebugTab comment and message for antigravity ([12398ab](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/12398ab1f4346400c71e4e2301a608a8fc02096e))
* **fleet:** populate synthetic token-card totals from usage_events ([#139](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/139)) ([709c6b5](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/709c6b5d7081b4dc5ae5a2cde572a78c35436a9b))
* **github:** prefer login over display name as identity/label when no email ([#145](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/145)) ([d061182](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/d0611828c1e9a1942e4bc69538a978ed15870ac9))
* **github:** surface swallowed collector errors and improve debug endpoint ([#142](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/142)) ([69ccd38](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/69ccd38b5228a544ae541f7ef836760e3c2ff73a))
* **mobile:** settings back-nav, token-health cards, about buttons, insights label truncation ([9a26e22](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/9a26e222122ad0ce4fb693d9dd85ef2230c29847))
* **mobile:** settings back-nav, token-health cards, about buttons, insights labels ([a574726](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/a574726f9d5d02b0089a56142bdf0b2786735c29))
* **overview:** show month by-model donut for token/spend providers ([#141](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/141)) ([83a580e](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/83a580edff92b49a1e06205d9fe1ff5dff5978cb))

## [2.5.0](https://github.com/s3ntin3l8/runway-ai-usage-tracker/compare/v2.4.0...v2.5.0) (2026-06-17)


### Features

* **dashboard:** show account identity on provider cards ([ee570d4](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/ee570d487ccaadf5be85cb8aa81facc709d102a8))
* **dashboard:** show account identity on provider cards for multi-account users ([#127](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/127)) ([11cff2f](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/11cff2f565c558a55e7625935b090a3d08ed4d09))
* **settings:** restore GitHub device flow login to provider settings UI ([#120](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/120)) ([0a3b7aa](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/0a3b7aa2b85333f68986a34572520f7c34b4ac87))
* **token-health:** sortable/filterable table + fix source-clobber cache bug ([#135](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/135)) ([5c02533](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/5c02533d7e15de20fc96ee8f885e1133b6d5a8a0))
* **token-health:** surface redundant flag, TTL, and generic account tokens ([40315fc](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/40315fcae3a5f011e492b98d4f13ccef99b48584))
* **token-health:** surface redundant flag, TTL, and generic account tokens ([#125](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/125) [#126](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/126) [#128](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/128)) ([68b73d3](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/68b73d3d7e7686ed2869a8f5b66e9f91aedb79e5))
* **token-health:** surface sidecar name in token cache UI ([#123](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/123)) ([4d8645b](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/4d8645bc26eceae5f12f662579b5f22f23c2d8e2))
* **tokens:** show sidecar originator as a badge ([#134](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/134)) ([8720e60](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/8720e6053d02f087ba5e04ce97dc108a21cd31eb))


### Bug Fixes

* **app:** mount TooltipProvider at root to fix blank /settings/tokens ([#133](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/133)) ([725539b](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/725539bf8387fd9f5f02f1dde3ed0aa50ac6e0e4))
* **debug:** add credential diagnostics to raw provider debug endpoint ([#119](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/119)) ([c103e3f](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/c103e3f470ff917453c16acd87b87bf7030820b7))
* **github:** handle new dict-shaped quota_snapshots API response ([#129](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/129)) ([94a5e9c](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/94a5e9ca06bc4314cc2a78708a8ed1085bc6a9bc))
* **home:** at-risk rail now features the window that is actually at risk ([#132](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/132)) ([cda76b6](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/cda76b6cfd0842a14183c2724658dc547c040b88))
* **tokens:** keep the freshest token in cache; stop stale sidecar pushes clobbering refreshes ([#124](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/124)) ([2e731ea](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/2e731ea2dcd3f8cbb6a5a86607cb294fe8f04bd3))

## [2.4.0](https://github.com/s3ntin3l8/runway-ai-usage-tracker/compare/v2.3.1...v2.4.0) (2026-06-17)


### Features

* **fleet:** add Update all button and rail update badge ([#115](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/115)) ([97466ef](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/97466efce3f4861a431a7ed9898551677fa932f4))


### Bug Fixes

* **gemini:** ship id_token from sidecar so quota resolves the email account ([#116](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/116)) ([834dbe2](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/834dbe255bd7511dc918c6253cb5908ea183d153))
* **github:** handle renamed Copilot quota keys and fix debug cache bypass ([#117](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/117)) ([549d395](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/549d3953fd872db643834da4e304c37de31cc331))
* **opencode:** always emit pct_used and unblock debug capture for sidecar-cookie providers ([#118](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/118)) ([9f7b72f](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/9f7b72f01b841d1ee00ce453e664203fd5ab4c4a))
* **sidecar:** normalize sidecar hostname to a stable id + merge duplicates ([#113](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/113)) ([d597de5](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/d597de535f2c72f321b94387e0fa51434776d172))

## [2.3.1](https://github.com/s3ntin3l8/runway-ai-usage-tracker/compare/v2.3.0...v2.3.1) (2026-06-16)


### Bug Fixes

* **sidecar-edge:** build only the tip of origin/main ([#106](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/106)) ([#110](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/110)) ([afbce9f](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/afbce9f2371b85739407fd508829a91a020d20f1))
* **sidecar:** prevent duplicate instance from Launch at Login ([#107](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/107)) ([#108](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/108)) ([91e958f](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/91e958f87f669a023b09b0667c78372becb4e6c6))

## [2.3.0](https://github.com/s3ntin3l8/runway-ai-usage-tracker/compare/v2.2.0...v2.3.0) (2026-06-16)


### Features

* **auth:** session-cookie admin auth with rotatable revocation ([#104](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/104)) ([e8fea8a](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/e8fea8aed4f91e092d14fe2dda6f3547d6ea150f)), closes [#92](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/92) [#100](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/100) [#103](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/103)
* **branding:** unify brand mark and add installable PWA ([#98](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/98)) ([c3028d5](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/c3028d541ddcf33fd19531db2ce3ddb5cc188a9d))
* capture working-directory/project context in all extractors ([#82](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/82)) ([b460aff](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/b460aff0c0e3c09b24cb3b5e162384abc9656bbc))
* cost views respect the Exclude cache toggle ([#79](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/79)) ([e801f47](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/e801f4748987fb71b1f07884da851c5b71b4603b)), closes [#73](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/73)
* cross-provider Top Models chart + global insights stats ([#81](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/81)) ([14e69a5](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/14e69a525e0d7fe2dc665790ecb90f0b2db7af4d))
* **insights:** cross-provider overall tokens & cost chart ([#95](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/95)) ([c57c355](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/c57c3557160424c2081e5b8de0da0f2ae4e8ad5b))
* per-category cost breakdown + exclude-cache in session/cost views ([#80](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/80)) ([216f427](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/216f4276f198eb8558bebba272dc23c35c1a59bd))
* **provider:** polish forecast picker, cost tab, and session detail ([#68](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/68)) ([008dfc2](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/008dfc2a075a5f2e733bdcf7d7d98336c15c3a7f))
* **provider:** selection-aware donut totals + exclude-cache toggle ([306bba7](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/306bba7aabb78bd50524f8f804b68e57b9dd52ae))
* **provider:** tab-level Month⟷Rolling time-scope toggle ([#87](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/87)) ([#97](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/97)) ([9b3f558](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/9b3f558b3111c89f4355a9bb06c38bd31a4380e8))
* surface project linking — Sessions tab, Top Projects, Top Tools ([#83](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/83)) ([b0834e2](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/b0834e23ddc4a5ce7a7ee666ed02504dae4fda1a))
* **ui:** exclude-cache toggle now applies to tokens-per-day charts ([#75](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/75)) ([ebb4d96](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/ebb4d962d5c3fac3c8f17733665ecdfadc24d8fc)), closes [#72](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/72)
* **ui:** extend "Exclude cache" toggle to all usage stat pages ([#67](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/67)) ([#69](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/69)) ([63724bf](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/63724bf2be2a97373638c1cabc05943e44f45ff9))
* **ui:** show originating sidecar in session lists ([#78](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/78)) ([040a42c](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/040a42c9672d3b03bf5d743c3a5351c37d57ae43)), closes [#71](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/71)
* **ui:** show server version at bottom of desktop rail ([f095bff](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/f095bff41fb0266c36d6b68aa7140acf3a8f020f))
* **ui:** token-composition bar + cache% in session info ([#76](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/76)) ([6f3c61e](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/6f3c61e2d5f04198d30cea39a650fc12fa2b6e94))
* **webapp:** promote Global Insights to its own /insights page ([#94](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/94)) ([ffe077e](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/ffe077e2b7c25dfe5af83ee0db2af9de53ac6f99)), closes [#85](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/85)


### Bug Fixes

* **charts:** polish activity-by-hour heatmap layout ([fa5731b](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/fa5731b4dd40700dbebfe6901a67b40b22a859a3))
* move Quota windows below history-graph stats on History page ([#88](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/88)) ([88135f9](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/88135f93e578de267cdbb3721a082e6c9af4da86))
* **security:** normalize blank secrets and fail fast on bad encryption key ([#105](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/105)) ([4e36103](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/4e361037c1872ab00ee1617b20fa33191dbe20f0))
* **sidecar:** document intentional empty-except in _release_lock ([44f0848](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/44f084884d64d00129689d90ed7c49df35b1fe25))
* **sidecar:** release self-update lock before re-exec so updates don't wedge ([#93](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/93)) ([b94672f](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/b94672fe6a0ac7b001b5433a4c49719df96786da))
* **sidecar:** retry transient GitHub errors during self-update ([#61](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/61)) ([82cb9c1](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/82cb9c1e4f292de7368c2c6af92dda1310ee2cbe))
* **updates:** follow GitHub repo rename so sidecars get update notifications ([#90](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/90)) ([273a6ca](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/273a6ca3ba11cea0ad466f46202cb1802308de70))

## [2.2.0](https://github.com/s3ntin3l8/runway/compare/v2.1.0...v2.2.0) (2026-06-14)


### Features

* **updates:** server-release banner + manual "check for updates" trigger ([#60](https://github.com/s3ntin3l8/runway/issues/60)) ([8596224](https://github.com/s3ntin3l8/runway/commit/8596224d10beefafb3854dfc10c330e8a5aaf6b6))


### Bug Fixes

* **e2e:** type screenshot.py so local mypy pre-commit hook passes ([#58](https://github.com/s3ntin3l8/runway/issues/58)) ([792a874](https://github.com/s3ntin3l8/runway/commit/792a8747ddc5f5dc457b0871195f38c866e9c066))

## [2.1.0](https://github.com/s3ntin3l8/runway/compare/v2.0.1...v2.1.0) (2026-06-14)


### Features

* **deploy:** dev/prod data split + compose override for existing Traefik ([#53](https://github.com/s3ntin3l8/runway/issues/53)) ([1454c7c](https://github.com/s3ntin3l8/runway/commit/1454c7ccde7d6eef68801b2e2640f0de29f4671f))


### Bug Fixes

* **sidecar:** bundle certifi + resolve TLS trust store for HTTPS pushes ([#57](https://github.com/s3ntin3l8/runway/issues/57)) ([03cf962](https://github.com/s3ntin3l8/runway/commit/03cf962b550259731c9f154381fef8eecf0a0c5b))

## [2.0.1](https://github.com/s3ntin3l8/runway/compare/v2.0.0...v2.0.1) (2026-06-14)


### Bug Fixes

* expose server version in /system/settings so About shows it ([#49](https://github.com/s3ntin3l8/runway/issues/49)) ([fef5689](https://github.com/s3ntin3l8/runway/commit/fef568926953349d4e2326f9eb50500da8c3cd42))

## [2.0.0](https://github.com/s3ntin3l8/runway/compare/v1.1.0...v2.0.0) (2026-06-14)


### ⚠ BREAKING CHANGES

* the server now serves webapp/dist (Vite/React SPA).
    - app/main.py: /assets mount with immutable caching, SPA catch-all for
      deep links (serves dist files for dotted paths, guards traversal),
      CSP tightened to script-src 'self' (no inline tz injection — the app
      fetches /system/app-config at boot), Google Fonts hosts removed
    - delete frontend/ + tests/frontend/ + node wrapper test (React escapes
      by default; the html.js innerHTML guard is obsolete)
    - Dockerfile stage 1 builds webapp; final image ships webapp/dist only
    - CI frontend job: working-directory webapp (typecheck + vite build + vitest)
    - Makefile: css/watch → web/web-dev/web-test; root package.json is a
      thin web:* delegator; CLAUDE.md + README updated

### Features

* **api:** offset/total pagination on /usage/events ([7a11fa1](https://github.com/s3ntin3l8/runway/commit/7a11fa1f4224ccb5d596a0d26ecaa2444ea33a57))
* cut over to the v2 SPA — retire the vanilla-JS frontend ([bc4d6aa](https://github.com/s3ntin3l8/runway/commit/bc4d6aa0a5dcd65cf3ad1d07255da64336e0c094))
* **fleet:** server-initiated sidecar auto-update + per-sidecar push ([d959215](https://github.com/s3ntin3l8/runway/commit/d959215f0bc16fd41ef4db4364c7665958bfbea6))
* **fleet:** server-initiated sidecar auto-update + per-sidecar push ([f7730d4](https://github.com/s3ntin3l8/runway/commit/f7730d4c138420d50a2d92a5b4beb9ccc7165c6f))
* **pricing:** add Claude Fable 5 pricing and fix model-name truncation ([1336c7a](https://github.com/s3ntin3l8/runway/commit/1336c7a126f05eaa92be1a6c5575800c7a08e5b9))
* **pricing:** add Claude Fable 5 pricing and fix model-name truncation ([d9a0108](https://github.com/s3ntin3l8/runway/commit/d9a010869d2c23d9f8822cda6ede7ece3b0c6a19))
* **sidecar:** classify from-source runs as edge when ahead of the latest tag ([c477246](https://github.com/s3ntin3l8/runway/commit/c477246b86c91bd6f9bdc1328ea991d08cd1d51e))
* **sidecar:** classify from-source runs as edge when ahead of the latest tag ([dab2790](https://github.com/s3ntin3l8/runway/commit/dab279092ab5f194ca29e4e5140fab2345d65b80))
* **sidecar:** rolling edge builds, dashboard update channel, CLI update check ([28075ab](https://github.com/s3ntin3l8/runway/commit/28075ab496c67554c9e3af425c8a92c8d6573894))
* **sidecar:** rolling edge builds, dashboard update channel, CLI update check ([5d8750b](https://github.com/s3ntin3l8/runway/commit/5d8750b1bfb5ab045dd030e915e263b7ee705658))
* **sidecar:** self-update — download, verify, swap & restart ([3538117](https://github.com/s3ntin3l8/runway/commit/35381171adfbdb4252a60c6cc8522f57eec66f1b))
* **sidecar:** self-update — download, verify, swap & restart ([a0dc603](https://github.com/s3ntin3l8/runway/commit/a0dc603b10195f1ff5e5d9d52f8d6b71760465d7))
* **ui:** show per-sidecar update channel in the Fleet view ([4152195](https://github.com/s3ntin3l8/runway/commit/41521957043280c317740d9f83a76349c3493922))
* **ui:** show per-sidecar update channel in the Fleet view ([b304f66](https://github.com/s3ntin3l8/runway/commit/b304f666f8f12a52d569910ed4e9d3bcf4fd8b4d))
* **webapp:** design-system primitives, chart theme bridge, dev kit gallery ([64a9f02](https://github.com/s3ntin3l8/runway/commit/64a9f022ab36d78510e49011ec6e637cd0eda7e0))
* **webapp:** fleet registry and settings sections ([9f32865](https://github.com/s3ntin3l8/runway/commit/9f328657f645d8cab14c3018bce6987f31447f1e))
* **webapp:** global month selector across provider detail tabs ([#48](https://github.com/s3ntin3l8/runway/issues/48)) ([3f0ae80](https://github.com/s3ntin3l8/runway/commit/3f0ae8090d91020bf53a5e130634eb1d1275e037))
* **webapp:** history view — fill curves, token/cost stacks, window archive ([2a5312e](https://github.com/s3ntin3l8/runway/commit/2a5312e638c0c6fd5a2c24d68f5aa365a1f729df))
* **webapp:** per-model token donut on the activity page ([f966c44](https://github.com/s3ntin3l8/runway/commit/f966c44d99d8f820b7d8447796310857edea7643))
* **webapp:** provider detail route — overview, activity, forecast, cost, debug ([7c3a67e](https://github.com/s3ntin3l8/runway/commit/7c3a67e1efc0f5924525d2ec315f396810a4c306))
* **webapp:** provider logos, expandable sessions, light-mode heatmap fix ([899db31](https://github.com/s3ntin3l8/runway/commit/899db312e086df4767e505af865002da4b8a8f90))
* **webapp:** restore sidecar update channel selector ([7b3c832](https://github.com/s3ntin3l8/runway/commit/7b3c8326f255c00f3e7e494d4fd0cdad62573e32))
* **webapp:** restructure provider detail view ([18653f4](https://github.com/s3ntin3l8/runway/commit/18653f4534b8be31f8f5cb0af8a3c1b66c1f5795))
* **webapp:** revamp provider detail — Events tab, richer Overview, glide-path forecast ([a6c6d8b](https://github.com/s3ntin3l8/runway/commit/a6c6d8bb17b79a211aec089bf05660dcbce3c0f1))
* **webapp:** risk-first home — at-risk rail, provider grid, aggregates, banners ([2606250](https://github.com/s3ntin3l8/runway/commit/2606250af55fa677a2890e16b06631a3a62d6ef8))
* **webapp:** scaffold UI v2 — Vite + React + Tailwind v4 with semantic theme tokens ([e7992d7](https://github.com/s3ntin3l8/runway/commit/e7992d75724a684d1e16a64220a2ece4443f1f8f))
* **webapp:** surface model on quotas, restore debug panes, fleet polish ([620c1db](https://github.com/s3ntin3l8/runway/commit/620c1db22981a41eecf7d35dae8e02ead9b69074))


### Bug Fixes

* **fleet:** channel persistence + gate update for non-self-updatable sidecars + card polish ([e42b224](https://github.com/s3ntin3l8/runway/commit/e42b224cc4f4b88155d564476321b4361af8dc0e))
* **fleet:** channel persistence, gate update for non-self-updatable sidecars, card polish ([7cca229](https://github.com/s3ntin3l8/runway/commit/7cca229a8ef1d8e4f0ad00a7679a196891a722a3))
* **fleet:** sanitize sidecar_id in update-push logs (CodeQL log injection) ([b2afaec](https://github.com/s3ntin3l8/runway/commit/b2afaece2eacda894a8cad49d409b7f5ba80e447))
* move EDGE badge to sidecar name + de-flake minute-bucket test ([05aa124](https://github.com/s3ntin3l8/runway/commit/05aa124e5c6fae80c2ad69054de84a3ed3b7c5c8))
* **sidecar:** resolve CodeQL findings in self_update ([a64f25f](https://github.com/s3ntin3l8/runway/commit/a64f25fcc4f6376688a862227a14a29a440ce48d))
* **sidecar:** resolve CodeQL findings in self_update ([0774b80](https://github.com/s3ntin3l8/runway/commit/0774b806b6909828eb22827658e98b86389792bf))
* **token_cache:** drop token-derived account_id from metadata log ([4fcf972](https://github.com/s3ntin3l8/runway/commit/4fcf972233f7bb191039b8568b76b2897c9259c9))
* **ui:** move the EDGE channel badge next to the sidecar name ([bbc4b8a](https://github.com/s3ntin3l8/runway/commit/bbc4b8a376b56ddccc46970f56f080629a6f0dc0))
* **webapp:** derive pct from used/limit, honor collector health, window chips ([1054c03](https://github.com/s3ntin3l8/runway/commit/1054c037c986f98bab14896770aecf148151c4e9))
* **webapp:** don't offer raw capture for sidecar-only providers ([25bbfa8](https://github.com/s3ntin3l8/runway/commit/25bbfa873784ae72e8c2454ac99b805640920460))
* **webapp:** readable activity heatmap in light mode ([5bfdb32](https://github.com/s3ntin3l8/runway/commit/5bfdb32bae4878d5019a536f606e8230316a3e80))
* **webapp:** settings nav appends segments and blanks content ([6ca8fcf](https://github.com/s3ntin3l8/runway/commit/6ca8fcf6544e15984a462f1a87c6366c30da9e83))
* **webapp:** stray scrollbar arrows on tab strips ([27b795e](https://github.com/s3ntin3l8/runway/commit/27b795e025acc9fcb4fdbf6a00c1f7d3de3759ff))
* **webapp:** use function-form manualChunks for Vite 8 Rollup typing ([10d274d](https://github.com/s3ntin3l8/runway/commit/10d274d2ec1e68d4c9b8d5a466e99d1cc2b011d6))

## [1.1.0](https://github.com/s3ntin3l8/runway/compare/v1.0.0...v1.1.0) (2026-06-09)


### Features

* **cost:** fall back to family rate for versioned model ids ([b4e1d7e](https://github.com/s3ntin3l8/runway/commit/b4e1d7eb6f251545a86c50d8a9281c856351db94))
* **display:** add selectable accent color themes ([8fc6c59](https://github.com/s3ntin3l8/runway/commit/8fc6c59fd3d81a7fce515552568db46a613d238b))
* **display:** replace letter-box provider badges with SVG icons ([653670d](https://github.com/s3ntin3l8/runway/commit/653670ddf2e9ab8dc8c4163fedee03a241365654))
* **fleet:** flag sidecars running an outdated release ([c956b6f](https://github.com/s3ntin3l8/runway/commit/c956b6f9ecc0f0a0e96ffb7148cd20b226660488))
* **frontend:** show Claude model version in model-mix legend ([020e091](https://github.com/s3ntin3l8/runway/commit/020e091c9e26fe690764d7bcb07b2c9d996d8956))
* **sidecar:** preserve Claude model version in model_id slug ([1ecabf1](https://github.com/s3ntin3l8/runway/commit/1ecabf1acd0819a9997f5205702f6141527a4aa9))
* **sidecar:** slim local settings UI, drop vestigial polling config ([2b9a242](https://github.com/s3ntin3l8/runway/commit/2b9a2425ad807cb40ea626e6b96ba6497afa4280))


### Bug Fixes

* add hidden username field to auth form for accessibility ([6d376c4](https://github.com/s3ntin3l8/runway/commit/6d376c45f0e443bde6146e19075153f83255e418))
* await checkAuth before initUI to prevent auth portal being hidden by race condition ([4f02127](https://github.com/s3ntin3l8/runway/commit/4f02127ceeb9edf6796862fab7c252fed8db5073))
* **ci:** gate on secrets instead of silently rewriting the baseline ([f460001](https://github.com/s3ntin3l8/runway/commit/f46000124578326b456316a33bcb1baa2db7603c))
* **ci:** gate on secrets instead of silently rewriting the baseline ([6b51ec5](https://github.com/s3ntin3l8/runway/commit/6b51ec54782dcf739d88bd0b064b03e6b3e70930))
* **codeql:** clear all 129 open code-scanning alerts in code ([#30](https://github.com/s3ntin3l8/runway/issues/30)) ([f33d8d5](https://github.com/s3ntin3l8/runway/commit/f33d8d54ad8c31edfac0b31c655cde7824b8386f))
* correct Anthropic Opus/Haiku seed rates to effective pricing ([c3dd778](https://github.com/s3ntin3l8/runway/commit/c3dd778f0a7494a1da9507e4d6314f08a81b2034))
* **display:** remove icon box border; add opencode-free icon mapping ([9217fcb](https://github.com/s3ntin3l8/runway/commit/9217fcb758a1f42562f65f1c40bd768486bd89ab))
* **display:** token expiry banner follows accent color ([f1b065e](https://github.com/s3ntin3l8/runway/commit/f1b065ecc088f6cb875c8ad0e683eee473775b8f))
* **display:** wire --accent-soft to --accent-dim so tints follow accent ([ab079a9](https://github.com/s3ntin3l8/runway/commit/ab079a9e98833449ecdf26143684c3be052364d9))
* **fleet:** always use calendar-month data for per-model and sidecar attribution ([27c68c6](https://github.com/s3ntin3l8/runway/commit/27c68c6afff451ec1a2eb363018a5b861fb6c7d2))
* **forecast:** align batch and per-card boundary-bucket handling ([e2b7a9c](https://github.com/s3ntin3l8/runway/commit/e2b7a9cfb51d890e1a09496cbf4d8c3f5c87080d))
* **forecast:** label insufficient-data cards "Gathering data" ([9ecb667](https://github.com/s3ntin3l8/runway/commit/9ecb6677bde8c3619953325d19db2e328ea4f1f5))
* **forecast:** report near-limit cards instead of "Won't exhaust" ([a987464](https://github.com/s3ntin3l8/runway/commit/a98746471f1a927bb68c968a986b7a818cafcaac))
* **frontend:** send admin key when fetching provider debug data ([e9c417a](https://github.com/s3ntin3l8/runway/commit/e9c417a729a5e4c7e151e1ef5bd5d71023a3486d))
* **frontend:** send admin key when fetching provider debug data ([2108586](https://github.com/s3ntin3l8/runway/commit/2108586bc6d79e217ae19a80cf49c927ac8f9cdd))
* **frontend:** vendor Sortable.js — CSP script-src 'self' blocked the CDN ([c137014](https://github.com/s3ntin3l8/runway/commit/c137014fbcfe7f4acd0fd477108a9f3c737d0939))
* remove Claude Design / Cowork quota cards ([cbebb36](https://github.com/s3ntin3l8/runway/commit/cbebb367b396d55d3b3730eab39cfb5a1f80c46a))
* reset "This period"/"Yearly" gauges on the user's local timezone ([8a444cc](https://github.com/s3ntin3l8/runway/commit/8a444ccc505c8a88f3fae16e363e35260904f9aa))
* **tokens:** forward codex refresh_token and suppress redundant expired-token banner ([f383b60](https://github.com/s3ntin3l8/runway/commit/f383b60f24fc126dedc03a5cee6a5e04beb62489))
* update Codex usage window from weekly to monthly ([5b6e0ed](https://github.com/s3ntin3l8/runway/commit/5b6e0ed1a27f3431e09583c1b69728e33f3d066b))
* vendor Sortable.js (CSP) + de-flake heatmap-tz tests ([32136af](https://github.com/s3ntin3l8/runway/commit/32136af09f5c4c2ea2f1e61c761510237b5c58b2))

## [1.0.0](https://github.com/s3ntin3l8/runway/compare/v0.13.0...v1.0.0) (2026-05-26)


### ⚠ BREAKING CHANGES

* **chatgpt:** Old _strategy_local_logs fallback removed

### Features

* accumulate per-model tokens in OpenCode by_model enrichment ([7b95643](https://github.com/s3ntin3l8/runway/commit/7b95643055479da125a3d78cd2035f65ccc543d3))
* **accumulator:** record quota snapshots on every upsert_latest_usage ([fa54596](https://github.com/s3ntin3l8/runway/commit/fa545962c0655f5cf2810bfc2ac41a6d82332966))
* add by_model aggregation to history endpoint ([9fa6ab7](https://github.com/s3ntin3l8/runway/commit/9fa6ab7df8141679baeb53bc17ddc8044ff62524))
* add CSS for expandable history rows ([e72c2fc](https://github.com/s3ntin3l8/runway/commit/e72c2fc9c2ca0763d48e90e4e919cda60928fdc9))
* **anthropic:** implement model-aware enrichment strategy ([f269841](https://github.com/s3ntin3l8/runway/commit/f2698411830bf8c692f032c5750fd77a1f556a8b))
* **antigravity:** surface card-only sidecars and standardize taxonomy ([5c6fdfb](https://github.com/s3ntin3l8/runway/commit/5c6fdfbe8b6e3c285bc1e15b39dca1898b7eabc6))
* **api:** /cumulative reads from usage_period_rollup with by_model/by_sidecar splits ([c80c4b0](https://github.com/s3ntin3l8/runway/commit/c80c4b0e644042b388cd0fbbf3973d4b4cf128f2))
* **api:** /fleet response includes per-account window_aggregations.longest ([9946e61](https://github.com/s3ntin3l8/runway/commit/9946e6130b95f7700352c0a43dfb4ce0f53a7606))
* **api:** /fleet sources sidecar_contributions from usage_period_rollup ([17521ec](https://github.com/s3ntin3l8/runway/commit/17521ecf5ad1c1641af67bdf7fc649d9bad985c5))
* **api:** /fleet/ingest accepts events[] and dispatches to EventIngestor ([2e86b88](https://github.com/s3ntin3l8/runway/commit/2e86b8898ffe808d4e9ce47a65f4ed6dfb3cf691))
* **api:** /fleet/ingest returns authoritative reset_anchors ([59b501a](https://github.com/s3ntin3l8/runway/commit/59b501aeabab00b381ec33753c8168afa26548a4))
* **api:** /usage/cost-forecast and /usage/anomalies endpoints ([a940327](https://github.com/s3ntin3l8/runway/commit/a940327721d930c647270db9297e195f5b4324bd))
* **api:** /usage/events endpoint with filtering and pagination ([081625b](https://github.com/s3ntin3l8/runway/commit/081625bf245f61a055c781cfc759a290789e756d))
* **api:** add /api/v1/usage/fleet — Most-Restrictive-Wins aggregation ([e4f4382](https://github.com/s3ntin3l8/runway/commit/e4f4382866503abda4fedd55f20a9c29c34de5c0))
* **api:** add /history/windows, /history/chart, /history/window-detail endpoints ([724034e](https://github.com/s3ntin3l8/runway/commit/724034e31cc1814bb1481f7781de4a7d4b395cc2))
* **api:** expose CumulativeUsage via /api/v1/usage/cumulative ([52c2fe3](https://github.com/s3ntin3l8/runway/commit/52c2fe3b1b2bde82a63ab4e08b310ac3e3f696fd))
* **api:** migrate /api/limits to read from LatestUsage table ([63bbe01](https://github.com/s3ntin3l8/runway/commit/63bbe01e22ae853b1b07d300658bb4ef0c54dc96))
* background OAuth token auto-refresh + modal dialogs + docs restructure ([cd49915](https://github.com/s3ntin3l8/runway/commit/cd499156645009cf8b80ffb52a14493446473391))
* **charts:** replace echarts legend with interactive HTML legend ([80c0731](https://github.com/s3ntin3l8/runway/commit/80c0731851c126a02e660021eabecb8a14a39bf1))
* **charts:** switch to hourly resolution for &lt;=7-day windows ([b410293](https://github.com/s3ntin3l8/runway/commit/b4102939f2753a870c9581f17fb76b4a735d84dd))
* **chatgpt:** add local enrichment strategy for Codex session logs ([423269b](https://github.com/s3ntin3l8/runway/commit/423269b8870ff4339d6988ed7a8046fdb7f92487))
* **collectors:** align all providers to Total Consumption token tracking ([a5f7c2a](https://github.com/s3ntin3l8/runway/commit/a5f7c2a2e6601449d55c2a5348aba33d822b3899))
* **collectors:** drop *_local mixins; server keeps api+web only ([93d7780](https://github.com/s3ntin3l8/runway/commit/93d77800f9bafb142821dc444f554a2bb5720fe3))
* **collectors:** drop server-side browser cookie scraping ([c184ca1](https://github.com/s3ntin3l8/runway/commit/c184ca1ca289b0554656cb3a4bd8b58162996696))
* **dashboard:** add forecast RISK/EXHAUST counts to fleet-strip ([280ec83](https://github.com/s3ntin3l8/runway/commit/280ec83d8f33cbb814341ae4b51c6c10274813cb))
* **dashboard:** aggregate shared quota pools + suppress misleading forecasts ([950fe89](https://github.com/s3ntin3l8/runway/commit/950fe89590767b077957dd6710734498f7ed3b46))
* **dashboard:** forecast marker on Fleet Commander gauges ([b393c80](https://github.com/s3ntin3l8/runway/commit/b393c801f1320235a92ab04151e18e033e9f3e5a))
* **dashboard:** forecast projection line on hero sparkline + percent/time/wording fixes ([c3cc43e](https://github.com/s3ntin3l8/runway/commit/c3cc43e879add4b82af57a196bb3ae00c6f435dc))
* **dashboard:** per-pool forecast badges and PAYG EoM forecast on Fleet Commander cards ([6931f2d](https://github.com/s3ntin3l8/runway/commit/6931f2d31fbf419aa873607717c536f28c534cb1))
* **dashboard:** token alert banner + debug modal expiry fix ([ea9ed77](https://github.com/s3ntin3l8/runway/commit/ea9ed770a45cc64d0df1af3f935edd52cd97c06a))
* **dashboard:** v2 redesign — horizon cards, HUD navbar, fleet health, race-condition fix ([5821c30](https://github.com/s3ntin3l8/runway/commit/5821c303bb67b939044f7aea73c0fcefe6d35172))
* **db:** add LatestUsage and CumulativeUsage tables ([dfa4a6e](https://github.com/s3ntin3l8/runway/commit/dfa4a6e9e5af74853c9d47b5a7a505a2798aef62))
* **db:** add QuotaSnapshot model for pct_used history ([7669884](https://github.com/s3ntin3l8/runway/commit/7669884ccb788619ec93104b42e7e8ac32bfa6eb))
* **db:** remove sidecar_id from unique constraints on latest_usage and cumulative_usage ([7b41618](https://github.com/s3ntin3l8/runway/commit/7b41618092bb12cddf9675dabcf41a777c74c6d1))
* **db:** wire pricing seed into init_db ([92a64c8](https://github.com/s3ntin3l8/runway/commit/92a64c8b1c93514498ffc9d6d74c4f040b386065))
* **events:** add kind column and error event emission for provider failures ([49a4aa8](https://github.com/s3ntin3l8/runway/commit/49a4aa8fd2b160812b5e2bbbd7ae6db20a6b86c6))
* **fleet:** centralized poll orchestration via _last_provider_polls ([422af26](https://github.com/s3ntin3l8/runway/commit/422af2667e006c6c3ccf2fd0b17b24bc352d4d0e))
* **forecast:** add model_id to all Anthropic card builders ([5e2cec2](https://github.com/s3ntin3l8/runway/commit/5e2cec29ed28d386055f98868d05dd5ce7ffc61b))
* **forecast:** comprehensive fix for Claude and OpenCode forecasts ([8c28f3c](https://github.com/s3ntin3l8/runway/commit/8c28f3c769fbf3c8b24274db5a15654db48c57be))
* **forecast:** extract trajectory mini-chart to shared component ([1066de8](https://github.com/s3ntin3l8/runway/commit/1066de8d008092548bc63756db0048e0d5f11440))
* **forecast:** pipeline refactor, anchor-at-now projection, drill-down, glide-path ([f007e5e](https://github.com/s3ntin3l8/runway/commit/f007e5e74c104751613eb60f95a27cfb42184553))
* **forecast:** remove forecast tab — signals relocated to dashboard and history ([6423888](https://github.com/s3ntin3l8/runway/commit/64238889a79ef697083c27716a4355228f099b57))
* **forecast:** rewrite forecast on quota-snapshots with Theil-Sen regression ([ff15841](https://github.com/s3ntin3l8/runway/commit/ff15841cb71041d5a6fd4a04e92ea8757ae0823b))
* **forecast:** rewrite forecast service against usage_events ([11c8b7e](https://github.com/s3ntin3l8/runway/commit/11c8b7eda3bd0db1f79348019a80cb08dbd1d133))
* **frontend:** add Display settings panel with 2-col, soft chrome, compact toggles ([3d4dd68](https://github.com/s3ntin3l8/runway/commit/3d4dd68af73246b5e2fdfa168492d0b72c4702e4))
* **frontend:** add fetchEvents/fetchWindowHistory/fetchHeatmap/fetchSessions clients ([b0d6297](https://github.com/s3ntin3l8/runway/commit/b0d6297b667c86b3cec11e5799c3368ce5af90f7))
* **frontend:** dashboard hero v4 redesign + frontend refactor + auth/perf hardening ([08d4a7c](https://github.com/s3ntin3l8/runway/commit/08d4a7c9d54a63b3b4ad4948eefdb9e4fb4100fd))
* **frontend:** Fleet Commander card sources by_model and fuel-dump from window_aggregations ([f806412](https://github.com/s3ntin3l8/runway/commit/f8064128d0b9958576c644bd1da46e6ac9d68faf))
* **frontend:** Run Now button shows spinner/check/error states ([18aa282](https://github.com/s3ntin3l8/runway/commit/18aa282bd44c7299fe7831d3adcf339a415b872d))
* **frontend:** use the pill logo as the header brand mark ([b0b31d8](https://github.com/s3ntin3l8/runway/commit/b0b31d8ec13eed0767a031f54f7aaf9fb9a70a76))
* **gemini:** enrich cards with token_usage and by_model via local session parsing ([db98611](https://github.com/s3ntin3l8/runway/commit/db98611554f6bb34213bf9e8ec5e25f5f7079079))
* **gemini:** populate token_usage and by_model via enrichment strategy ([a6c8f24](https://github.com/s3ntin3l8/runway/commit/a6c8f24fd8101051fa9beb650c779c0f21c570c4))
* history page window-first redesign ([ebaaf91](https://github.com/s3ntin3l8/runway/commit/ebaaf917d866d55ceda3f715008474bc0ae1bb81))
* **history:** add account column to usage snapshot table ([a081eb2](https://github.com/s3ntin3l8/runway/commit/a081eb235a78e6b8138f97c057ece09ae3b5cee3))
* **history:** add summary tiles and /history/deltas endpoint ([6881311](https://github.com/s3ntin3l8/runway/commit/68813116001bc20e84a0e4e55231d8921c8ccca1))
* **history:** add token usage and cost tracking to history views ([1db1e00](https://github.com/s3ntin3l8/runway/commit/1db1e00eeba3b6ec8ee082bd328fec087ea06233))
* **history:** clarify cache semantics across chart, tile, and sparkline cards ([0d8e0b4](https://github.com/s3ntin3l8/runway/commit/0d8e0b4171691387dc82d9ee1728cb5343fb9e66))
* **history:** dashed projection overlay on percent chart when window type is selected ([26eebb3](https://github.com/s3ntin3l8/runway/commit/26eebb397de03e1844721c16a287dce1b935b867))
* **history:** flat snapshot table with tokens, cost, and live badges ([68059a6](https://github.com/s3ntin3l8/runway/commit/68059a6df6697dfd7a034b031467738a486d3a56))
* **history:** inline cache show/hide toggle on the chart ([a2b3fe4](https://github.com/s3ntin3l8/runway/commit/a2b3fe412ae487e9508ca0ab700e2f302e248fc7))
* **history:** restore Chart.js chart style with new data source ([d30539a](https://github.com/s3ntin3l8/runway/commit/d30539a318ad2159e750514e464c1e69f3bf09ba))
* **history:** v2 HUD restyle — hud-panel chrome, seg controls, HUD table ([4bb7940](https://github.com/s3ntin3l8/runway/commit/4bb794018ef528fe3661c3bbf5b53176935c6178))
* **identity:** add resolve_account_id canonical account identity resolver ([ace8f3a](https://github.com/s3ntin3l8/runway/commit/ace8f3a35d092a7bb10867cfbab2ff8456978816))
* **identity:** wire resolve_account_id and merge_card_json into LatestUsage and CumulativeUsage write paths ([2203bea](https://github.com/s3ntin3l8/runway/commit/2203bead8e6b121aa38a60b028a4cd03719d2fc0))
* include per-model tokens in Anthropic by_model enrichment ([78c422b](https://github.com/s3ntin3l8/runway/commit/78c422b618eda4580bef08cff51170601905cb63))
* include per-model tokens in Gemini by_model enrichment ([477e686](https://github.com/s3ntin3l8/runway/commit/477e686a637a32009c0ae4d4dcc33f67ad4971ec))
* **ingest:** add EventIngestor with dedup, cost calc, and rollup updates ([2215150](https://github.com/s3ntin3l8/runway/commit/2215150eebd8839d2b0f347e1ffb5fca3223cee7))
* **ingest:** support deltas and poller db upsert ([90cf122](https://github.com/s3ntin3l8/runway/commit/90cf122b9d4615341ff22307d7f8227b21be1959))
* **merge:** add merge_card_json for fusing server and sidecar card payloads ([ff25736](https://github.com/s3ntin3l8/runway/commit/ff25736c17f7b361952e94933e3ec8f8fb350a43))
* **modal:** "models" section label on session cards ([68e25c6](https://github.com/s3ntin3l8/runway/commit/68e25c65955c7c73fb229db43420e3ad10f7e953))
* **modal:** add .m-detail CSS rule for session card second row ([a4f7018](https://github.com/s3ntin3l8/runway/commit/a4f7018f0472dfca690e01e4ad1ae3a3e694cd64))
* **modal:** add hover tooltips to hour-of-day heatmap ([7e671db](https://github.com/s3ntin3l8/runway/commit/7e671dbc5893d918c9e1fc76256ecd6c9621aef7))
* **modal:** add hover tooltips to monthly cost chart ([23404a3](https://github.com/s3ntin3l8/runway/commit/23404a3a2d22084ce143ebd6dfe339b8cfcbbd6e))
* **modal:** add quota trajectory section to provider modal Overview tab ([b1cac1f](https://github.com/s3ntin3l8/runway/commit/b1cac1fa84ee0cca7bd4a683e2fe258058f4a639))
* **modal:** cost pane with monthly bars, forecast, sidecar breakdown ([01e6d36](https://github.com/s3ntin3l8/runway/commit/01e6d36928049421335eee4b753d651374cf5fe4))
* **modal:** debug pane with provider metadata, sidecars, token health ([eadb315](https://github.com/s3ntin3l8/runway/commit/eadb315a4de08f654a5dc4e25d3813d952e9aff8))
* **modal:** enrich session cards with title, token breakdown, cache hit % ([7e41beb](https://github.com/s3ntin3l8/runway/commit/7e41beb0468338f59740d558c6661ba3b1422764))
* **modal:** overview pane with hero, KPIs, model mix, sidecars, history ([825de1e](https://github.com/s3ntin3l8/runway/commit/825de1e1ecebeed2ea1f1ef5d1e56f21c2dc5af0))
* **modal:** per-model breakdown rows on session cards ([8fe2a2f](https://github.com/s3ntin3l8/runway/commit/8fe2a2f717f9eef6b58ba8cb3a6e0c279c5c23cc))
* **modal:** provider detail modal scaffold + tab switching ([4223afd](https://github.com/s3ntin3l8/runway/commit/4223afd90468b0ab320c0ee1f80b7cdb519058cf))
* **modal:** replace events log with recent session cards in overview pane ([14eff5a](https://github.com/s3ntin3l8/runway/commit/14eff5a09da473cf76fed441f62be0cc0623fb68))
* **modal:** usage pane with heatmap, top sessions, throughput ([7a9d775](https://github.com/s3ntin3l8/runway/commit/7a9d775c84b1218102abc583dae4d64cbe361124))
* **modal:** wire overview tab to fetch 3 recent sessions ([ed56f2b](https://github.com/s3ntin3l8/runway/commit/ed56f2b9a755dd5fc41fe282cd7e1722c11101ed))
* **observability:** audit log, PII redaction, dashboard view ([c205610](https://github.com/s3ntin3l8/runway/commit/c2056101e8c32af891aa9a8bc94605f21c2dd0c6))
* **opencode:** add token breakdown fields and fix window type detection ([06c779b](https://github.com/s3ntin3l8/runway/commit/06c779bc224416f869391dd47b59c39d07d7cb83))
* **pricing:** add ChatGPT gpt-5.x model pricing and cost backfill script ([e44a900](https://github.com/s3ntin3l8/runway/commit/e44a900b3f2d720e0e4d5db84ff476b0da2ec481))
* **pricing:** add cost_calculator with effective-from price lookup ([a90c41b](https://github.com/s3ntin3l8/runway/commit/a90c41bf81deb9a977069152cdf11029d55672b4))
* **pricing:** seed provider_pricing with public rates ([d5a7d0c](https://github.com/s3ntin3l8/runway/commit/d5a7d0c8642040d152f58d0489a0ea830681cba9))
* **query:** add query_window_aggregation for per-window usage rollups ([16036a1](https://github.com/s3ntin3l8/runway/commit/16036a1111ab9488fd394ada6fa91b643f2adbe9))
* **query:** add query_windows, query_chart, query_window_detail ([fda9153](https://github.com/s3ntin3l8/runway/commit/fda9153690bccb67f3c6361db5b9ebcd99fdbd45))
* restore history & forecast pages after event-sourced refactor ([427417d](https://github.com/s3ntin3l8/runway/commit/427417d7700a63294fea284d83ffb6ecf695a14c))
* rewrite history table with expandable rows ([456d1e7](https://github.com/s3ntin3l8/runway/commit/456d1e767a7b7f0b630d63633d96025dcf526703))
* **rollups:** incremental period rollup updates per event ([b44566f](https://github.com/s3ntin3l8/runway/commit/b44566f86c1f900586625d073c17106dc3c547b3))
* **schemas:** replace UsageDelta with UsageEventPush in IngestRequest ([2048e08](https://github.com/s3ntin3l8/runway/commit/2048e08f64ceb30f95f9029bf168a2ca411684bf))
* **security:** multi-host startup gates + standard response headers ([c80a70c](https://github.com/s3ntin3l8/runway/commit/c80a70c2acabce75505a56496ee1bd7c43cc07c5))
* **server:** drop keychain access from collector_manager + credential_provider ([9126f39](https://github.com/s3ntin3l8/runway/commit/9126f396601ec9c10d72ad4047af9028d044879b))
* **services:** implement UsageAccumulator for delta processing ([e8df13f](https://github.com/s3ntin3l8/runway/commit/e8df13f8e5471554ccece2503087ad0e2b557102))
* **sessions:** add sort_by=recent param to sessions endpoint ([46747d0](https://github.com/s3ntin3l8/runway/commit/46747d0a9052f41e0d6b78759a013981a8a1b7d4))
* **sessions:** add token breakdown and cache hit % to session query ([94b24ec](https://github.com/s3ntin3l8/runway/commit/94b24ec5c964f80e8605af19358e4a7fec3a11dc))
* **sessions:** tag and surface subagent activity on session cards ([ba74ff9](https://github.com/s3ntin3l8/runway/commit/ba74ff91221c729c23bbc13140fe973fe1dd63ef))
* **settings:** v2 HUD restyle + accordion provider rows ([8d40256](https://github.com/s3ntin3l8/runway/commit/8d40256fb370bb144ecbead39f9867d02156336e))
* **sidecar:** add Anthropic event extractor ([96cc06b](https://github.com/s3ntin3l8/runway/commit/96cc06b7cf237a97f4156a5946c72ee283fdd736))
* **sidecar:** add ChatGPT/Codex event extractor ([b628c46](https://github.com/s3ntin3l8/runway/commit/b628c46f78160f28064860dd7149d9de1ee138ee))
* **sidecar:** add Gemini event extractor ([613e28a](https://github.com/s3ntin3l8/runway/commit/613e28ab60c51b33e9000e5b65396f954a7d1bb6))
* **sidecar:** add OpenCode event extractor with provider-supplied cost ([e775ea5](https://github.com/s3ntin3l8/runway/commit/e775ea5a8fe31ab43944a63e425ffba7f4c3fc03))
* **sidecar:** emit Anthropic events instead of cumulative deltas ([d05717f](https://github.com/s3ntin3l8/runway/commit/d05717f47423cd0fa6ae0348d27f7dd3282efd31))
* **sidecar:** emit email as account_id for Gemini and Codex enrichment from id_token ([819f3fe](https://github.com/s3ntin3l8/runway/commit/819f3fe357f4a8b93a6d591669a924ebf24651c0))
* **sidecar:** emit tokens_*/cost_usd deltas for Fleet HUD Wingman Pods ([c05b7ed](https://github.com/s3ntin3l8/runway/commit/c05b7eda358ee8569eeab2aee3e1b4a5adeb0737))
* **sidecar:** heartbeat-driven cadence so refresh button feels instant ([ac11c1e](https://github.com/s3ntin3l8/runway/commit/ac11c1ea86f43e4ec27d5767f007dc8a0cbf5d9e))
* **sidecar:** honor RUNWAY_API_URL / RUNWAY_API_KEY env vars ([895fb24](https://github.com/s3ntin3l8/runway/commit/895fb24757bdc590011bf9643ed448d9c17d5703))
* **sidecar:** implement usage delta tracking ([383d639](https://github.com/s3ntin3l8/runway/commit/383d6398c517ad445042126eeadc3d331f5d89ea))
* **sidecar:** log reset_anchors from ingest response ([4aee88c](https://github.com/s3ntin3l8/runway/commit/4aee88c34b1583bcd0d80f614042e0485b50c132))
* **sidecar:** persistent event-push watermark ([ec9e0e4](https://github.com/s3ntin3l8/runway/commit/ec9e0e4ed412ac78e5f9f4e9c3c6f8ae73bb36d1))
* **sidecar:** port Anthropic JSONL token enrichment from main ([cef7268](https://github.com/s3ntin3l8/runway/commit/cef7268d7b09b54297901933431c076fc069dad2))
* **sidecar:** port Gemini session token enrichment from main ([a22ecce](https://github.com/s3ntin3l8/runway/commit/a22ecce6f2e9fe202ba448f7c28fd31543554b31))
* **sidecar:** port OpenCode token enrichment from main ([216aafe](https://github.com/s3ntin3l8/runway/commit/216aafe2ab74bcec42d32bbdc45f7ef2cd3782a1))
* **sidecar:** propagate discovered account identity onto provider cards ([4298d75](https://github.com/s3ntin3l8/runway/commit/4298d75faaf4f7f3b04d424baae6aaa6612b4665))
* **sidecar:** ship Linux binaries (tray + headless CLI) ([f4c3541](https://github.com/s3ntin3l8/runway/commit/f4c354164de65b40a9330bebf26541464861cb5b))
* **sidecar:** wire chatgpt/gemini/opencode event extraction ([b86b3eb](https://github.com/s3ntin3l8/runway/commit/b86b3eb2862923b71d51015c2b8fa5773613694e))
* **spark:** add _buildQuotaSparkSvg and _buildXLabelsFromPoints for overview pane ([34b5b4e](https://github.com/s3ntin3l8/runway/commit/34b5b4e011587f15028f8e94524d17d0a7f7f944))
* **spark:** add _niceMax and _fmtTick axis scale helpers ([933fcd2](https://github.com/s3ntin3l8/runway/commit/933fcd27e4f18f41d1f66b3ec75d42fe87dd0297))
* **spark:** add hover tooltip showing time label and token count ([21f2718](https://github.com/s3ntin3l8/runway/commit/21f27184dec60976664cca9306751c01d5227406))
* **spark:** add X-axis label builder and axis/tooltip CSS ([9e74a30](https://github.com/s3ntin3l8/runway/commit/9e74a307e508a84a8dffef741340208c75e011ba))
* **spark:** compute Y grid lines from niceMax, return {svgHtml, yTicks, series} ([da7726e](https://github.com/s3ntin3l8/runway/commit/da7726ec5b459e423a068082cb0d70806ae81e95))
* **spark:** fetch quota chart data for overview tab and wire hover tooltip ([d13ed78](https://github.com/s3ntin3l8/runway/commit/d13ed78ee045516461b5828bdef6fec1bdb8eb0d))
* **spark:** rewrite wireOverviewSparkTabs and add wireOverviewSparkHover ([3e31da7](https://github.com/s3ntin3l8/runway/commit/3e31da7551448f1bad0b5f249527d711e124d1c0))
* **spark:** wire quota sparkline structure into buildOverviewPane ([e66c898](https://github.com/s3ntin3l8/runway/commit/e66c8982db91dedfa6f3f0b5ff04f3838cef8329))
* **spark:** wire Y-axis labels, X-axis labels, and unit meta into tab switching ([83439f8](https://github.com/s3ntin3l8/runway/commit/83439f8b7cb8abbb4032ac3b7a50a2c3465f75a4))
* standardize token counting and display across all collectors ([4ff65d6](https://github.com/s3ntin3l8/runway/commit/4ff65d643ee3d9346b9b2f18547a6bae561d14ed))
* **tz:** render timestamps in user's local timezone ([3db6d1f](https://github.com/s3ntin3l8/runway/commit/3db6d1f398286fe887d0d7b4f74ec794096375a2))
* UI-driven poll cadence + sidecar pause/resume ([da8bb10](https://github.com/s3ntin3l8/runway/commit/da8bb10d394bba705f34ff6f05eacde9f8d0b5db))
* **ui:** add dimension filters (account, window) to dashboard ([edefe54](https://github.com/s3ntin3l8/runway/commit/edefe54230b9fcc40e864be32dfe4bb76dc0d467))
* **ui:** add hero-equal class for equal width columns ([aaaf134](https://github.com/s3ntin3l8/runway/commit/aaaf134efb4c0cb99e835df6babee30d24153f51))
* **ui:** add styled tooltip to provider status light ([8fe15f0](https://github.com/s3ntin3l8/runway/commit/8fe15f0ff3f2e77cb973c8f45c7d62a89d0b9eea))
* **ui:** Fleet HUD frontend — Fleet Commander cards, Wingman Pods, Fuel Dump bar ([4ecaaa1](https://github.com/s3ntin3l8/runway/commit/4ecaaa1284a4d0992b8c84b17671395cbcf26d54))
* **ui:** implement Fleet HUD glide paths and Velocity cards ([3a5869d](https://github.com/s3ntin3l8/runway/commit/3a5869dd34b3ca43ae0ad8084c71531a76266b8b))
* **ui:** implement hero layout for forecast status summary ([a6be988](https://github.com/s3ntin3l8/runway/commit/a6be988ea7e25e7a08027ef98b78b4c1e1848bde))
* **ui:** increase forecast chart vertical space ([51c420e](https://github.com/s3ntin3l8/runway/commit/51c420e65166e457166fc0f53ac0ff8f38bd7d9c))
* **ui:** make forecast hero boxes equal size ([4680511](https://github.com/s3ntin3l8/runway/commit/46805114344e07ac989cf92cd08c9563a9156640))
* **ui:** match forecast table styling with history view ([4784007](https://github.com/s3ntin3l8/runway/commit/4784007cb1ebfced74eefadac72a024e9fc56386))
* **ui:** redesign forecast layout to match history view ([da4b380](https://github.com/s3ntin3l8/runway/commit/da4b380e38910f1db04151507f3401f7691912b2))
* **ui:** replace forecast dropdowns with dashboard-style chips ([8f9f954](https://github.com/s3ntin3l8/runway/commit/8f9f9543f3c4a1267dd9f9bd7760c69ee1d88602))
* **ui:** show token total on cards with token_usage data ([c428a47](https://github.com/s3ntin3l8/runway/commit/c428a47a38893471b2c8aabb000aa007488eb7a2))
* **ui:** split forecast kpis into critical and healthy groups ([399f3ec](https://github.com/s3ntin3l8/runway/commit/399f3ec9a2c3567594802e5c0073d123709b5f32))
* **ui:** widen detail modal from max-w-4xl to max-w-5xl ([a4052f0](https://github.com/s3ntin3l8/runway/commit/a4052f0aa3b5c0ac3d14dc11bae3a0b937f0f807))
* **windows:** add close_window aggregation ([a36d69d](https://github.com/s3ntin3l8/runway/commit/a36d69d6a41b00bfe86c503bcaf6bb9bdee9be21))
* **windows:** detect window-close at LatestUsage upsert ([de1d1f1](https://github.com/s3ntin3l8/runway/commit/de1d1f10059a9a3afa7d5664afd24925321027b1))


### Bug Fixes

* **accumulator+charts:** self-heal stale window_type rows + hard cap session X-axis ([0dedf0c](https://github.com/s3ntin3l8/runway/commit/0dedf0ca214becf36fb1565093559668790b8cac))
* **accumulator:** clear stale error stamps when fresh quota arrives ([c9cccc4](https://github.com/s3ntin3l8/runway/commit/c9cccc47948199262cab3daf5b886fedd786b3f3))
* **accumulator:** evict stale aggregate error cards when per-model data arrives ([f96418c](https://github.com/s3ntin3l8/runway/commit/f96418c0ca81a62d411aa67c4530ca40abbca18d))
* **accumulator:** guard cross-window-type delete for aggregate cards (model_id='') ([6f4cdd5](https://github.com/s3ntin3l8/runway/commit/6f4cdd546234bc048f2c95e5a921e62b4960a281))
* **accumulator:** satisfy mypy on delete().where() column comparisons ([fab4507](https://github.com/s3ntin3l8/runway/commit/fab4507303048d0a6ff29968db4f09aca85cba48))
* add missing provider_id to aggregated OpenCode cards and add frontend safeguards ([b531f3f](https://github.com/s3ntin3l8/runway/commit/b531f3f1972596c51e2cec1cf359785467322363))
* **anthropic:** deduplicate cards and simplify _primary_strategy ([7a6db8b](https://github.com/s3ntin3l8/runway/commit/7a6db8bb36655fe1138d81272ad44fcce7d16ed9))
* **anthropic:** implement global error prioritization and fix Claude CLI fallback ([e39267f](https://github.com/s3ntin3l8/runway/commit/e39267fd5d9b6916254181b34e8424cd36e0063f))
* **anthropic:** skip unknown null API keys to prevent duplicate cards ([571aebf](https://github.com/s3ntin3l8/runway/commit/571aebf5d27e4d40157184cec540d4c561fa8aee))
* **anthropic:** update Claude Design card title and fix token leakage ([b066fc8](https://github.com/s3ntin3l8/runway/commit/b066fc82df3a0322c5bc9109710cecaa6eb8ceed))
* **anthropic:** use primary card reset_at for enrichment window boundaries ([5464fea](https://github.com/s3ntin3l8/runway/commit/5464feabbdb95f225aa9589ae53e5a8e22f8d637))
* **antigravity:** emit account_id from sidecar; drop dead server collector ([5ddd203](https://github.com/s3ntin3l8/runway/commit/5ddd20376657b74df6966d52a8ae0757703ff97c))
* **antigravity:** include passive providers in poll cadence + prune ghost rows ([a659688](https://github.com/s3ntin3l8/runway/commit/a659688bc49d7a77857adf542b9673c65ea5c761))
* **antigravity:** only show card when IDE running or quota file exists ([4cfcd71](https://github.com/s3ntin3l8/runway/commit/4cfcd71890699eeb7abbe6cb937f2d79bf99665f))
* **antigravity:** parse ISO 8601 resetTime from LSP ([7d964d5](https://github.com/s3ntin3l8/runway/commit/7d964d5c6d38405cf0e1fc964b23c30926046ad2))
* **antigravity:** treat all MODEL_* internal ids as placeholders ([6727096](https://github.com/s3ntin3l8/runway/commit/6727096aa66cad791dd1eabbb239f429e694577b))
* capture primary metadata in chatgpt collector ([96fd7e4](https://github.com/s3ntin3l8/runway/commit/96fd7e493445d0793e862684d64cd4d2323b5112))
* **chart:** correct session forecast horizon, anchor, and add end labels ([c6ffb7e](https://github.com/s3ntin3l8/runway/commit/c6ffb7eff5fe47e64c5cdc2ef8e355f347b28e9d))
* **charts:** containLabel true and locale-aware tooltip formatting ([eee41c4](https://github.com/s3ntin3l8/runway/commit/eee41c45b46d534e8b6cd73e51aa56cf42da228a))
* **chatgpt:** roll forward old window reset in local enrichment ([9bd979f](https://github.com/s3ntin3l8/runway/commit/9bd979fe9adb9e1efb05790140923df789f4a33a))
* **chatgpt:** support chunked NextAuth.js session tokens and oai-sc cookie ([b2f6c4b](https://github.com/s3ntin3l8/runway/commit/b2f6c4ba26f938b6d8de7dd70e3c086d9b4bbeba))
* **collectors:** wrap sync file I/O in async paths (audit H5) ([1d6ed9e](https://github.com/s3ntin3l8/runway/commit/1d6ed9e2b9f3f82dd244910ca44e0c5e00a43417))
* **config:** unify config dir to runway (from runway-tracker) ([dc4b91e](https://github.com/s3ntin3l8/runway/commit/dc4b91e95c1c066b30059a9fc4e12ce75421cc89))
* correct provider attribution and unit formatting for intercepted error cards ([f7ab49b](https://github.com/s3ntin3l8/runway/commit/f7ab49b933328777188c35d3aaeadd56253b11a7))
* **dashboard:** center + flow-wrap the 2-col empty cume subtitle ([e004766](https://github.com/s3ntin3l8/runway/commit/e004766c425ae8cdd487a67256710cc4792759fb))
* **dashboard:** collapse empty cumulative tray to one full-width row ([cd92b01](https://github.com/s3ntin3l8/runway/commit/cd92b01b00820ba216936375df4b2a098dd42ee8))
* **dashboard:** fix PAYG badge z-index and unify single-quota card layout ([8756e43](https://github.com/s3ntin3l8/runway/commit/8756e438ac67f31aba3309a713457ab4ab511d11))
* **dashboard:** force 2-line empty cume in 2-col mode ([55fdf7a](https://github.com/s3ntin3l8/runway/commit/55fdf7ae4a97a9c6c94fa1f77d5dd001a8833e29))
* **dashboard:** keep empty cumulative label on one line, split sub for 1-col ([6ba9631](https://github.com/s3ntin3l8/runway/commit/6ba9631b46a5596eccf67d9e3b68c00f4dfe953e))
* **dashboard:** make empty cumulative tray text larger and breathe ([54e2e72](https://github.com/s3ntin3l8/runway/commit/54e2e727d3ae706b04403c6520a160b84f11c20b))
* **dashboard:** make every cume row 3 lines for visual consistency ([e9de3ba](https://github.com/s3ntin3l8/runway/commit/e9de3ba159dde6618366edd4020359154b441bda))
* **dashboard:** move glide-path comment below meter bar in pool rows ([a0f087e](https://github.com/s3ntin3l8/runway/commit/a0f087eaf1e6df0492efb0516104ef5d4505e0fc))
* **dashboard:** push fc-velo cells below PAYG badge; add glide status to pool rows ([67895b9](https://github.com/s3ntin3l8/runway/commit/67895b9139e45c4dace62456ea2908f8ff1246fc))
* **dashboard:** stretch empty-cume row so subtitle can wrap, not overflow ([cb2e1c1](https://github.com/s3ntin3l8/runway/commit/cb2e1c12de052a7f154ef29d4eccce182a12c56a))
* **dashboard:** suppress modal-open click during edit/drag mode ([7511442](https://github.com/s3ntin3l8/runway/commit/751144234b5edd3572ecc42db6c5e01aedfb4d41))
* **dashboard:** surface reset time + EXHAUSTED label on fully-drained crit cards ([b3d1c9d](https://github.com/s3ntin3l8/runway/commit/b3d1c9db366d10e058adf4812e8bd589b836923d))
* **dashboard:** token banner link, text polish + session chart 12h guard ([4f61e5d](https://github.com/s3ntin3l8/runway/commit/4f61e5d96316da606705deeae0a984284252b63d))
* **dashboard:** unset .card .sub ellipsis on empty cumulative subtitle ([5234f26](https://github.com/s3ntin3l8/runway/commit/5234f267c89f60cc469397e77aabd8b28f4b7375))
* **dashboard:** wire drag-to-reorder for fleet commander cards ([85eb4c8](https://github.com/s3ntin3l8/runway/commit/85eb4c8a16157a375e33c90b9660292087adc1d8))
* **db:** add model_id to LatestUsage unique constraint via table recreation ([0b74e41](https://github.com/s3ntin3l8/runway/commit/0b74e41073c93afcf7ea8fefc7c9577f3e5bea33))
* **db:** apply WAL/busy_timeout at connect, drop BEGIN IMMEDIATE ([ab9c56c](https://github.com/s3ntin3l8/runway/commit/ab9c56c8ac6e170b0bc19c44aa7130b7fd2d501f))
* **db:** stub broken endpoints and remove compaction after schema reset ([706eabc](https://github.com/s3ntin3l8/runway/commit/706eabcbe1e4e61e927bb7cb431dcb3161df0085))
* **extractors:** stop double-counting cache tokens for gemini + chatgpt ([5d9599b](https://github.com/s3ntin3l8/runway/commit/5d9599b501b67eda40accfda779f2a682ac1595f))
* **filterbar:** keep search box on same row when chips overflow ([727b202](https://github.com/s3ntin3l8/runway/commit/727b2028046d1356b4e10c4c94886f167764d093))
* **filterbar:** prevent chip text wrap and hide scrollbar ([35d6ffe](https://github.com/s3ntin3l8/runway/commit/35d6ffeeedc49461d3488fe0e79337f71efbfec3))
* **fleet:** align provider_ids and unblock per-model dashboard data ([26e98d1](https://github.com/s3ntin3l8/runway/commit/26e98d1997c1d0227ce9706d32ed6850eede7b1c))
* **forecast:** ensure ECharts library loads before rendering chart ([7997d37](https://github.com/s3ntin3l8/runway/commit/7997d37949bc8bcc6468f1e76b4739c94582f813))
* **forecast:** remove service_name from identity key for quota matching ([d64cdd2](https://github.com/s3ntin3l8/runway/commit/d64cdd21a453e0f2b2dde32dd69e2910c11425ef))
* **forecast:** resolve mypy union-attr and ruff E711 for SQLAlchemy NULL check ([6c6bf71](https://github.com/s3ntin3l8/runway/commit/6c6bf71f86549e8942b2c8cde39ed584162c09a3))
* **forecast:** trim batch snapshot cache to current window before regression ([51a997a](https://github.com/s3ntin3l8/runway/commit/51a997ad13a19c8fc2da89162888aa4ea31472be))
* **frontend:** use the pill logo as favicon ([f4a596f](https://github.com/s3ntin3l8/runway/commit/f4a596f0988af23ab9ec6572df00ae6ef3d09387))
* **gemini-oauth:** close the refresh loop so tokens stop flipping to expired ([9717126](https://github.com/s3ntin3l8/runway/commit/9717126954c3e66e33f923109c4d1b51c796506b))
* **gemini:** emit versioned 3.x model ids and add Gemini 3 pricing ([4ead1b3](https://github.com/s3ntin3l8/runway/commit/4ead1b37ac874c5a488f578227e2a371d1746002))
* **gemini:** extract account label from OAuth id_token ([06f8090](https://github.com/s3ntin3l8/runway/commit/06f80901d225746479b290b6ed444325984f1965))
* **gemini:** inherit account_label from cache metadata in fallback token path ([553512d](https://github.com/s3ntin3l8/runway/commit/553512dd66e66b4b400411285ea1fb7838c97ec0))
* **gemini:** keep the bucket carrying real quota when classes collide ([5240544](https://github.com/s3ntin3l8/runway/commit/5240544875104e988ea81ec06d0edb2995a14520))
* **gemini:** safe timestamp compare, dedupe model mapping, fix smoke test ([bf2c23f](https://github.com/s3ntin3l8/runway/commit/bf2c23faa58ab43452aba8125f114cde6f674ff2))
* **history:** apply provider filter client-side on snapshot table ([638020f](https://github.com/s3ntin3l8/runway/commit/638020fac51c03c2c8a034e87752a2f55ed03c3f))
* **history:** associate model breakdowns with specific windows ([c869db6](https://github.com/s3ntin3l8/runway/commit/c869db6ced125576e5cba4d96a977159412964b2))
* **history:** bucket fill series by window type, not calendar day ([f9b18fb](https://github.com/s3ntin3l8/runway/commit/f9b18fbe1306ee2f6c313c17610c4529268b9bb1))
* **history:** chart loading, fill dedup, variant labels, empty expand ([534e5c1](https://github.com/s3ntin3l8/runway/commit/534e5c11678de082b4b771baae61213f6cb7d597))
* **history:** connect forecast overlay to historical line end ([63b7ade](https://github.com/s3ntin3l8/runway/commit/63b7ade9e4a8b7932b19e42a2e045ff4fbad732c))
* **history:** correct burn rate, label ranges, per-model snapshot rows ([f5434bd](https://github.com/s3ntin3l8/runway/commit/f5434bdd1972430c9c6a8443105a5978b9b8e11d))
* **history:** correct pct_used derivation and deduplicate closed windows ([1064710](https://github.com/s3ntin3l8/runway/commit/1064710ad036f6699509e0c7868e83d9338f8f42))
* **history:** dedup open/closed windows, days filter, split columns ([13631a1](https://github.com/s3ntin3l8/runway/commit/13631a1714d62be74c11376be5a00ee0cee585e2))
* **history:** emit explicit UTC offset on naive snapshot timestamps ([d5abafc](https://github.com/s3ntin3l8/runway/commit/d5abafc7afa7cd51d2bbc9122b1da049c311f1fc))
* **history:** hide Services pills with no data in selected window ([74e8dfe](https://github.com/s3ntin3l8/runway/commit/74e8dfee1332f3134a22903e812c41aa3c6aae76))
* **history:** hide window selector on tokens/cost charts ([d5139e6](https://github.com/s3ntin3l8/runway/commit/d5139e650b3b4a180ef8c2f8c3aa416dc7ee4d2d))
* **history:** hide zero-usage sparkline cards + diagonal cache stripes ([9e25d45](https://github.com/s3ntin3l8/runway/commit/9e25d45a644312176ae337c963b5f2119250212e))
* **history:** improve data fidelity by fixing compaction data loss and delta truncation ([029dac2](https://github.com/s3ntin3l8/runway/commit/029dac21316ffde771290208f378bd1b4507a822))
* **history:** prevent metric double-counting via hierarchy filter ([55699d1](https://github.com/s3ntin3l8/runway/commit/55699d1611a885bb94ab9fc77c17f0f315ce3cbd))
* **history:** render per-model quota lines in chart ([77e043b](https://github.com/s3ntin3l8/runway/commit/77e043b85f1535bfc7c194306b6c7266edcdc49b))
* **history:** server-side multi-provider filter so pagination matches visible rows ([a0181ee](https://github.com/s3ntin3l8/runway/commit/a0181ee91d8f011cdd88cc0bdb2b2ce79da94be6))
* **history:** show fill series for all providers, match selected time range ([3f2d68f](https://github.com/s3ntin3l8/runway/commit/3f2d68f6aafa924228034c6d47aff85ac469c36f))
* **history:** shrink TIME column to content width ([b0e4df1](https://github.com/s3ntin3l8/runway/commit/b0e4df1cfc9833c7d8a518ee2c7e77e05a46b781))
* **history:** store arrays per window slot, add monthly column, filter by_model to session windows ([09365e3](https://github.com/s3ntin3l8/runway/commit/09365e36410ce4c72d9852f8e0b761329e71d65c))
* **history:** suppress burn rate inflation via first-read baseline and flattened payload ([0a127e0](https://github.com/s3ntin3l8/runway/commit/0a127e053fb2ce16a4bfd5938acd2069376fed43))
* **history:** surface mid-bucket peaks and fix zero-pct pagination ([18af59b](https://github.com/s3ntin3l8/runway/commit/18af59baa8315a74a26eb4ac80f24405df7cd691))
* **history:** use getUserTz() resolution chain for all date display ([040e46e](https://github.com/s3ntin3l8/runway/commit/040e46e165946bd6c5c44930882d946b25c10e19))
* **identity:** improve email regex and remove redundant condition ([b073c90](https://github.com/s3ntin3l8/runway/commit/b073c90fa02625dd05dcbfcb38861d70aad26f3d))
* **identity:** thread account_label through UsageDelta for consistent canonical_account_id resolution ([09bbcee](https://github.com/s3ntin3l8/runway/commit/09bbceee555a346d3703322e9942c2e087260476))
* **identity:** widen TLD regex and document provider_id parameter ([6ef2625](https://github.com/s3ntin3l8/runway/commit/6ef2625ae40c83a2e39fa9c751cb86e46d440968))
* **ingest:** atomic batch ingest + concurrency-safe rollups ([c18cbf7](https://github.com/s3ntin3l8/runway/commit/c18cbf775435c90a996a6845e7bf66e330cae23f))
* **ingest:** rate-limit POST /ingest at 600/minute per source IP ([5b95a14](https://github.com/s3ntin3l8/runway/commit/5b95a147805b3fb9d44fa2cf526ab3fd912fdcfb))
* **ingest:** wake poller on token push, force collector re-sync ([3f09c44](https://github.com/s3ntin3l8/runway/commit/3f09c448a937e729df1a87ee8358394b398db7fc))
* **lint:** apply ruff format to satisfy CI formatter check ([a1b1baf](https://github.com/s3ntin3l8/runway/commit/a1b1baf3c9d9034b920bf0fec15f8408b21c7e42))
* **lint:** sort datetime imports in recost_events.py ([9a9ac1b](https://github.com/s3ntin3l8/runway/commit/9a9ac1b30580ac94b719748fb3f4313d8592193b))
* **logging:** render server + sidecar log timestamps in user's local tz ([2175b7a](https://github.com/s3ntin3l8/runway/commit/2175b7a298fec047002219ee360c00a5de89a96e))
* **merge:** fix _join_distinct to split comma tokens before dedup ([ff12388](https://github.com/s3ntin3l8/runway/commit/ff123885bfe8893682e02ad5a47b8560785c1675))
* **modal:** compute window-reset countdown from reset_at, not the reset label ([eca5f8c](https://github.com/s3ntin3l8/runway/commit/eca5f8c09d0535cab76561a626f45fae9e601af2))
* **modal:** kill horizontal scrollbar in provider detail modal ([32e18e0](https://github.com/s3ntin3l8/runway/commit/32e18e00d7c6a7912e9194c115135c0789365ad5))
* **modal:** kill horizontal scrollbar in provider detail modal ([06a9863](https://github.com/s3ntin3l8/runway/commit/06a98631f50c31ca00f47e69052876e2c585f5d7))
* **modal:** normalise heatmap API dicts in sparkline and heatgrid builders ([c1924f3](https://github.com/s3ntin3l8/runway/commit/c1924f3f3ed48cf0e564a015aad69cbb0f25c225))
* **modal:** populate debug timing fields and fix token health filter ([29e07b7](https://github.com/s3ntin3l8/runway/commit/29e07b7d95755989392384c0d4eb2016872cdeef))
* **mypy:** resolve type errors in event_query and credential_provider ([c25d760](https://github.com/s3ntin3l8/runway/commit/c25d760ad753fc2a035aedb2f63314459a92b028))
* **mypy:** silence call-overload false positives in history SQL select ([52dc77a](https://github.com/s3ntin3l8/runway/commit/52dc77a043c1eea5e452f92bcf23728322f51f04))
* **mypy:** suppress SQLAlchemy/SQLModel false-positive type errors ([c2443c8](https://github.com/s3ntin3l8/runway/commit/c2443c89ad5adfa8288c89a7a44d3920702456c4))
* **oauth:** add token-endpoint backoff and gate aggressive recovery on 429 ([e103ba7](https://github.com/s3ntin3l8/runway/commit/e103ba75f282e5dbff380f1ea61a49592229a296))
* **opencode:** correct pct_used, remove debug dump, and add web API tests ([b2607c1](https://github.com/s3ntin3l8/runway/commit/b2607c1296dbc5817e2cb50527086b329175fa23))
* **opencode:** deduplicate fleet cards from stale pre-fix sidecar events ([e925bbd](https://github.com/s3ntin3l8/runway/commit/e925bbd927eb66470c073cd74ffac7ec39bbf00a))
* **opencode:** keep synthetic free-tier entry alive after a rollup wipe + fix modal model-mix fallback ([a650bfd](https://github.com/s3ntin3l8/runway/commit/a650bfdf8796946e1e40c06fc2f41c3f77efb7ac))
* **opencode:** robust model naming and server-side identity propagation ([14a3402](https://github.com/s3ntin3l8/runway/commit/14a3402e76aaf1d8f6db3047739b9c0c4753e2f0))
* **opencode:** split free-tier events + show calendar-month per-model strip ([3559e26](https://github.com/s3ntin3l8/runway/commit/3559e26847e3764e78718f9a190a269d04e518d8))
* **opencode:** support new React Suspense format for /usage page ([dc8195f](https://github.com/s3ntin3l8/runway/commit/dc8195f1f94781ae49046d01b6f5133481da2cb2))
* **poller:** repair closed-transaction bug and source label accuracy ([aa1c1c6](https://github.com/s3ntin3l8/runway/commit/aa1c1c69dcb558cc864c01daa8bd31bc07ebc161))
* **poller:** upsert collected cards into LatestUsage; fix force-collect ([796ccb6](https://github.com/s3ntin3l8/runway/commit/796ccb6e490603cdeed554bdfc0b72601bbbc20e))
* preserve token_usage and msgs in history additional entries ([4db90f7](https://github.com/s3ntin3l8/runway/commit/4db90f738b085adbc29fb6a4d769c6349dc995c9))
* **pricing:** align Gemini cost calculation with official rates ([d2f993d](https://github.com/s3ntin3l8/runway/commit/d2f993d57557a65e9979c8739de17f47919a9c30))
* **quota:** cluster pools by explicit pool_id, not behavioral similarity ([cf0fe1c](https://github.com/s3ntin3l8/runway/commit/cf0fe1ce1609279e4deb7b6aa9acb5dd84cc1800))
* roll forward old window reset in gemini and anthropic local enrichment ([0a9b994](https://github.com/s3ntin3l8/runway/commit/0a9b994eb35c97dd8dfc33885741ce8540ac89e1))
* security and reliability hardening from code review ([2bdf65c](https://github.com/s3ntin3l8/runway/commit/2bdf65c4e3bda1f55e7d9514110f530dff861a22))
* **sessions:** add tokens_reasoning to breakdown, tighten cache_hit_pct test ([f924676](https://github.com/s3ntin3l8/runway/commit/f9246761154078eb2afafeca9e27b6e1a2337b96))
* **sessions:** split cache read/write, replace hit% with cache% of total tokens ([1f017b5](https://github.com/s3ntin3l8/runway/commit/1f017b563174f30124a90a1b92ea4e9a6b191107))
* **settings:** eager-load nav counts + show real strategy IDs in provider meta ([c4e88db](https://github.com/s3ntin3l8/runway/commit/c4e88db8479c6b8012650435176b3ea8ed49e10b))
* **settings:** fix host/port mono spacing and browser-pref input overflow ([e20a12f](https://github.com/s3ntin3l8/runway/commit/e20a12f22e13cb695e5191a9f24166508c775ef1))
* **settings:** prevent timezone select from overflowing the control column ([f7f40fe](https://github.com/s3ntin3l8/runway/commit/f7f40fe2e3040d0741647de3edecc88df37cb173))
* **settings:** replace misleading strategy meta with effective poll interval ([b5600c0](https://github.com/s3ntin3l8/runway/commit/b5600c0ea8d31961d7e7091151493c666bb704f1))
* **sidecar:** align config + HMAC secret with the project's .env ([4ffe14e](https://github.com/s3ntin3l8/runway/commit/4ffe14e70b57f9913f9f62a3860d30785cf1daf3))
* **sidecar:** always classify Antigravity as weekly for history continuity ([c7ea177](https://github.com/s3ntin3l8/runway/commit/c7ea177f4628ad7c92fbffd3d29129e042270b40))
* **sidecar:** coerce antigravity model_id, harden clock-skew detection ([287d63d](https://github.com/s3ntin3l8/runway/commit/287d63d5ab4b311d19c31a1e1fb05030bb94c64d))
* **sidecar:** drop deprecated *_jsonl_enrichment rules from registry ([1fb3c9c](https://github.com/s3ntin3l8/runway/commit/1fb3c9c940c3e80c4128aeeb7482ed7e3a4afc7c))
* **sidecar:** emit 100% card for Antigravity models with exhausted quotaInfo ([e794e68](https://github.com/s3ntin3l8/runway/commit/e794e68cf3c6f83272f094c3ab5651a223e58953))
* **sidecar:** emit critical health for exhausted Antigravity cards ([c9bf3a6](https://github.com/s3ntin3l8/runway/commit/c9bf3a6bd290e9f00b12513edc08696e22535baa))
* **sidecar:** infer Antigravity window_type dynamically from reset_at duration ([1f64758](https://github.com/s3ntin3l8/runway/commit/1f647585aa16e0721fcb53b09b4086d020fbd0b6))
* **sidecar:** pass -a to lsof so port discovery is scoped to the LSP PID ([ae08766](https://github.com/s3ntin3l8/runway/commit/ae087668303e9806544d060eb2f972e484480b03))
* **sidecar:** repair scripts.sidecar_pkg import + batch event posts; bump server body cap ([bd9cc22](https://github.com/s3ntin3l8/runway/commit/bd9cc22d93181b1ec366c3283447a6a6bd5c1f75))
* **sidecar:** report a real version instead of "unknown" ([6875b07](https://github.com/s3ntin3l8/runway/commit/6875b071872a2ed8f60b6edde06599e6055b9660))
* **sidecar:** scope Antigravity pool_id by quota family ([31f6522](https://github.com/s3ntin3l8/runway/commit/31f65227363ddd0928b31bd37e1700b0329a339c))
* **sidecar:** use OpenCode account email instead of hardcoded "default" ([34d5c16](https://github.com/s3ntin3l8/runway/commit/34d5c162177cc636f7db109ddc2142c579205cf4))
* **sidecar:** window Gemini token enrichment to 24h and align with daily quota cards ([5da73ec](https://github.com/s3ntin3l8/runway/commit/5da73ece8487316ae6d1db3e0c9d90ba7f556954))
* **test:** move DaemonRunner test out of TestCodexAccountEmail to correct class ([7a172a6](https://github.com/s3ntin3l8/runway/commit/7a172a6f464785b8790d91f08ab41f133c9bc69e))
* **test:** parameterize table name in migration test SQL helper ([371c300](https://github.com/s3ntin3l8/runway/commit/371c300f5ef6e49641d4aec471b84b2f8e14773a))
* **tests:** reset slowapi rate-limit counters between integration tests ([8dbab11](https://github.com/s3ntin3l8/runway/commit/8dbab11ea9a6e75d8aff76e7c98c95af051ab8c8))
* **tests:** update test_ingest_invalid_payload to reflect optional metrics field ([0ec3365](https://github.com/s3ntin3l8/runway/commit/0ec3365d2037e02edc5c38c191aedef939f2f6a4))
* **tests:** use relative timestamps and mock Session in poller wake test ([c4030da](https://github.com/s3ntin3l8/runway/commit/c4030da2846e7fff7d6aa6fd0283a3c4ceb68976))
* truncate long card subtitles to prevent badge overflow ([3a77134](https://github.com/s3ntin3l8/runway/commit/3a77134b018b2437f566e590d482a8d69bc8e973))
* truncate long card titles and subtitles to prevent badge overflow ([e71959c](https://github.com/s3ntin3l8/runway/commit/e71959ccf6433cafc110256f9f86206e5587b813))
* truncate long card titles and subtitles to prevent badge overflow (input.css) ([eed3495](https://github.com/s3ntin3l8/runway/commit/eed34951d01175d19f9faa0dfe0c6b18bf07d46b))
* **tz:** inject timezone config synchronously into HTML on page serve ([31b9740](https://github.com/s3ntin3l8/runway/commit/31b974012e58a49ef32e0e10487f1e9564a40071))
* **ui:** fix status light tooltips and cleanup dead code ([2715cb4](https://github.com/s3ntin3l8/runway/commit/2715cb4cb78b518d9d8009f49d9d01c2f211e028))
* **ui:** improve reset time display with today/tomorrow labels ([d9f9ac4](https://github.com/s3ntin3l8/runway/commit/d9f9ac421b2beae270c983f797256b983b8106ac))
* **ui:** locale-aware number formatting in provider sparkline strip ([189fbd9](https://github.com/s3ntin3l8/runway/commit/189fbd9b8d86f9ba19f2e3f2302b7edb5eb35514))
* **ui:** remove timezone suffix from local time display ([e4c4289](https://github.com/s3ntin3l8/runway/commit/e4c42894d0778ec5619473b893ee54c53c9a8e59))
* **ui:** restore relative time on card HUD, keep formatResetDisplay for modal only ([a2c1c2c](https://github.com/s3ntin3l8/runway/commit/a2c1c2c51eafe879fc1b9931d2de618debb2d6a4))
* **ui:** use formatResetDisplay for modal reset field ([cbd3a43](https://github.com/s3ntin3l8/runway/commit/cbd3a43e3b69ef0b1106e237fa41334b04cfa8b2))
* **window:** exclude kind='error' events from window aggregations ([3cf5cb4](https://github.com/s3ntin3l8/runway/commit/3cf5cb4e271b358fe96578adbe19ef6280c14698))
* **xss:** escapeHTMLAttr also escapes \", &, &lt;, &gt; ([c04d3ff](https://github.com/s3ntin3l8/runway/commit/c04d3ff60f270afc6c5428262ee56e29b8ce67cf))


### Performance Improvements

* **history:** SQL-based aggregation for dramatic performance improvement ([f316fe8](https://github.com/s3ntin3l8/runway/commit/f316fe8d6bb6cc6d6b5978be117bbf7d938982e8))
* **history:** SQL-side snapshot bucketing + client SWR cache ([4cfe78f](https://github.com/s3ntin3l8/runway/commit/4cfe78fd8e514cd5e1ad701ad04684003c7bac40))
* **queries:** batch N+1 in query_snapshots live windows (audit H2) ([c5b640c](https://github.com/s3ntin3l8/runway/commit/c5b640c2a685452284b914d274dd78faf8dfb776))


### Reverts

* **compaction:** restore main's bucket_fn implementation ([5f99b87](https://github.com/s3ntin3l8/runway/commit/5f99b872071bf4f1453025674542194075a66a1a))

## [0.13.0](https://github.com/s3ntin3l8/ai-usage-tracker/compare/v0.12.2...v0.13.0) (2026-04-24)


### Features

* account label overlay, special-model window routing, and history table rework ([a78dafe](https://github.com/s3ntin3l8/ai-usage-tracker/commit/a78dafe3df19dc24981cf46a9fc2dede1f286a60))
* Add enrichment pattern for combining API + local token data ([62193be](https://github.com/s3ntin3l8/ai-usage-tracker/commit/62193be613de7e38852cdabbcb2e6e5002d89fa6))
* **anthropic:** enrich API/web quota cards with local log token data ([78fa375](https://github.com/s3ntin3l8/ai-usage-tracker/commit/78fa3755b1a53cebe4e21e15b3c59f7835154dcd))
* **anthropic:** fold model-specific seven-day quotas into weekly window type ([bb09bd2](https://github.com/s3ntin3l8/ai-usage-tracker/commit/bb09bd2a62d830d8b0a49ca2ada772a0bd2abeb8))
* **antigravity:** enrich card fields with provider_id, account_label, model_id, used_value, reset_at ([c57ce6a](https://github.com/s3ntin3l8/ai-usage-tracker/commit/c57ce6a31e883cd6ed928ba972accf6574fd5602))
* **claude:** optimize web api collector and improve raw data debugging ([6d27af0](https://github.com/s3ntin3l8/ai-usage-tracker/commit/6d27af0db882092b23f2bd4f335b67c09649c637))
* **claude:** support credits format in extra_usage ([00899ce](https://github.com/s3ntin3l8/ai-usage-tracker/commit/00899ce7e82e10a68681e694c9cef7a2f639b171))
* **collectors:** standardize architecture and align file naming conventions ([1b95f78](https://github.com/s3ntin3l8/ai-usage-tracker/commit/1b95f7809904c295418f66183069550a6cc10557))
* **config, docs:** Unify config dir to 'runway-tracker' and enhance collector docs ([6fd2c10](https://github.com/s3ntin3l8/ai-usage-tracker/commit/6fd2c10343b480ed483bdc21ee66d729dd78e639))
* **external_metrics:** deduplicate antigravity cards across sidecars, keep latest per account ([d4e4110](https://github.com/s3ntin3l8/ai-usage-tracker/commit/d4e4110d577b993b8e6221e988df28646f221ea0))
* **forecast:** add forecast service, schemas, and unit tests ([9a5f747](https://github.com/s3ntin3l8/ai-usage-tracker/commit/9a5f7471adb3e338cf0580a63589643d2a4c148d))
* **forecast:** add FORECAST SPA view with KPI strip, table and chart ([bd16dec](https://github.com/s3ntin3l8/ai-usage-tracker/commit/bd16decf9e7b83dbc484c9d2754fcb20deece0d8))
* **forecast:** add GET /api/v1/usage/forecast endpoint + integration tests ([ce4632e](https://github.com/s3ntin3l8/ai-usage-tracker/commit/ce4632ebf3f61ab9e4cd8039f71ff4e21b4e46b4))
* **forecast:** add GET /api/v1/usage/forecast endpoint with filter tests ([7f1659e](https://github.com/s3ntin3l8/ai-usage-tracker/commit/7f1659eab165bcb04c0a0214cdeeb3976f26fa0d))
* **forecast:** add inline forecast projections to dashboard cards ([959ff74](https://github.com/s3ntin3l8/ai-usage-tracker/commit/959ff749cae13e32826daacaeef1aff014ecf3c4))
* **forecast:** add inline forecast projections to dashboard cards ([eb55832](https://github.com/s3ntin3l8/ai-usage-tracker/commit/eb55832efd244e8761c258998722093993ad652f))
* **forecast:** add usage forecasting feature ([a62806d](https://github.com/s3ntin3l8/ai-usage-tracker/commit/a62806d67aa96698f88bf2bd7f9bb0127b079d83))
* **identity:** implement unified identity extraction and fix account label persistence ([49e243d](https://github.com/s3ntin3l8/ai-usage-tracker/commit/49e243db0358f1b66db51c3fd586b69968485f3c))
* implement aggressive Anthropic token refresh on 429 errors ([16e82e4](https://github.com/s3ntin3l8/ai-usage-tracker/commit/16e82e4a6284322428bf8cab701355c9eda20fa1))
* implement auth portal and fix internal oauth connection ([f857238](https://github.com/s3ntin3l8/ai-usage-tracker/commit/f85723827098b7468ef61ed14029a7b08f867561))
* **opencode:** enrich web-API cards with local DB token breakdown ([9432013](https://github.com/s3ntin3l8/ai-usage-tracker/commit/943201313ca52054fbec7d26a80957ef4e7d0bb1))
* **opencode:** manual auth cookie + /usage page Free/API tier cards ([2ba126b](https://github.com/s3ntin3l8/ai-usage-tracker/commit/2ba126b4dac37f48451e6c553127159fd00617cb))
* **sidecar:** add circle outline to macOS menubar icon ([d77fb3a](https://github.com/s3ntin3l8/ai-usage-tracker/commit/d77fb3a426d20f70fc627f4eaddccadfbe12df6c))
* **sidecar:** add kimi_coding provider entry to registry ([39b6d73](https://github.com/s3ntin3l8/ai-usage-tracker/commit/39b6d733e03888c0741f2639e4d25f37538d8ab9))
* **sidecar:** browser-based settings UI ([85857c5](https://github.com/s3ntin3l8/ai-usage-tracker/commit/85857c519ae5fac504e23dc842038704f3ddc06d))
* **sidecar:** clean white template icon for macOS menubar ([c4a1e8c](https://github.com/s3ntin3l8/ai-usage-tracker/commit/c4a1e8c5f7d73bcf4c3fe7b7db9dc9f6af70c2cd))
* **sidecar:** detailed per-provider logging + antigravity LSP probing ([7ab206a](https://github.com/s3ntin3l8/ai-usage-tracker/commit/7ab206aa045a82cd2acd902c4cb814edf1196e3f))
* **sidecar:** enrich antigravity cards with structured fields, fix file paths ([7dc98e8](https://github.com/s3ntin3l8/ai-usage-tracker/commit/7dc98e821742f2e2aa06a082b0346b5f18877f99))
* **sidecar:** fleet management features — version reporting, stale alerts, remote trigger, hot reload, Linux autostart ([0fd11b2](https://github.com/s3ntin3l8/ai-usage-tracker/commit/0fd11b22e7806773ca2676f4cb078e4af5e2074d))
* **sidecar:** increase settings page width from 480px to 600px ([5132e96](https://github.com/s3ntin3l8/ai-usage-tracker/commit/5132e96003edbc15e2bbc052a8eafe64578d20aa))
* **sidecar:** logging, fleet registration, about dialog, logo icon with status dot ([99e0c26](https://github.com/s3ntin3l8/ai-usage-tracker/commit/99e0c266b390c7738e8b79ee03f7d903cd56ffd1))
* **sidecar:** settings improvements, log viewer, provider toggles, fleet logs ([e43d174](https://github.com/s3ntin3l8/ai-usage-tracker/commit/e43d1742ffdd08ea3c2777cb88984f0fb5a04c8a))
* **source:** split data_source mechanism and input_source origin ([8d7cf1e](https://github.com/s3ntin3l8/ai-usage-tracker/commit/8d7cf1e95d2f33792427b87e74c2269db0a433ea))
* standardize polling intervals and enhance UI transparency ([ee538c8](https://github.com/s3ntin3l8/ai-usage-tracker/commit/ee538c821bf7b9e647bb89ef1ae91a4cc7f2480a))
* **token-health:** show token source (sidecar vs local) in Token Health panel ([4986f99](https://github.com/s3ntin3l8/ai-usage-tracker/commit/4986f9920fba6e33a2fd9368b91316e73a941d86))
* **token-health:** surface ProviderConfig API keys and session cookies ([a323772](https://github.com/s3ntin3l8/ai-usage-tracker/commit/a3237725579b5c4c68fe2e5d3975f9f8747ee3f7))
* **ui:** add peak tracking to history charts ([34d0574](https://github.com/s3ntin3l8/ai-usage-tracker/commit/34d0574bfd0b381cd3887fe5360f98fc8a78796c))
* **ui:** aviation HUD redesign — B612 Mono, amber phosphor, CSS token system ([c1f6a62](https://github.com/s3ntin3l8/ai-usage-tracker/commit/c1f6a627566b4b2c9dd6a1f35def56e740474ed6))
* **ui:** refine Token Health dashboard and account identity discovery ([5442df5](https://github.com/s3ntin3l8/ai-usage-tracker/commit/5442df5fd4cd421fd9fff77f7dc9ca3c54bd5f6a))


### Bug Fixes

* account_label, chart windows, credit providers ([a20ea82](https://github.com/s3ntin3l8/ai-usage-tracker/commit/a20ea827006577468dd52b5d26e09f31ec22a968))
* add raw history endpoint, fix chart data flow, improve formatting ([7539740](https://github.com/s3ntin3l8/ai-usage-tracker/commit/7539740344e9156c6904c59ba7a0dfd917fd9853))
* **anthropic-oauth:** include seven_day_omelette in window name_map and core_keys ([286f72e](https://github.com/s3ntin3l8/ai-usage-tracker/commit/286f72e06d83e15fa8c97300860cdcd0e0f72341))
* **antigravity:** fix _format_reset guard, add missing credit card keys, add sub-minute display ([4d34154](https://github.com/s3ntin3l8/ai-usage-tracker/commit/4d341546b279c5f0d3fda57cf66f2ca04805aa41))
* chatgpt account_label always default + raw data view invisible ([7c8fe50](https://github.com/s3ntin3l8/ai-usage-tracker/commit/7c8fe5063283edb33cd70314208e424080108534))
* **claude:** improve tier extraction and add debug labels ([d9f30c5](https://github.com/s3ntin3l8/ai-usage-tracker/commit/d9f30c5f24bebd17d06b753cb6ade6509c684176))
* **collectors:** guard tier_label NameError when extra_data is present without windows ([c45e04d](https://github.com/s3ntin3l8/ai-usage-tracker/commit/c45e04dfb4bf5f9a5c688fad3586a5ba8db60f81))
* **collectors:** hold reference to fire-and-forget create_task in anthropic_web ([6051ee9](https://github.com/s3ntin3l8/ai-usage-tracker/commit/6051ee9043a2d48cb71060a20ce48fe5113d182d))
* **external_metrics:** always copy card before appending to candidates ([b01173c](https://github.com/s3ntin3l8/ai-usage-tracker/commit/b01173c80ac675150817a05b71fe6f4df6de0343))
* **forecast:** align filter guards with history endpoint, patch public API in tests ([1244437](https://github.com/s3ntin3l8/ai-usage-tracker/commit/12444374906739d4e04de428fd9d612ab4fef166))
* **forecast:** prevent provider filter self-narrowing on filtered reload ([31f0f3f](https://github.com/s3ntin3l8/ai-usage-tracker/commit/31f0f3f053e27ad73c47c744251be59d9cb09348))
* **forecast:** report near-zero projections as stable (fixes Claude appearing unchanged) ([70fb664](https://github.com/s3ntin3l8/ai-usage-tracker/commit/70fb6647d532121ffc452cea21a88f7b01e44809))
* **forecast:** require 4 samples before projecting, show stable without number ([d29c87a](https://github.com/s3ntin3l8/ai-usage-tracker/commit/d29c87a49eba19795b39c72dca5f441f63681fec))
* **forecast:** resolve mypy type errors and strengthen percent-unit test ([b0f7155](https://github.com/s3ntin3l8/ai-usage-tracker/commit/b0f71553547701ee9bb798aa2ce236eb80e9cdd2))
* **frontend:** correct '&lt;' escape entity in two view modules ([be14a36](https://github.com/s3ntin3l8/ai-usage-tracker/commit/be14a3654ef4c6903c5215bb3e22814d4398a297))
* **frontend:** pass event explicitly to handleResetProvider instead of reading window.event ([b3b5454](https://github.com/s3ntin3l8/ai-usage-tracker/commit/b3b5454a4abf3073fa09ca47743125797e5f0e89))
* **frontend:** register chart resize listener once at module load ([686f67a](https://github.com/s3ntin3l8/ai-usage-tracker/commit/686f67a215a771f2b64b938692d178baa48bf655))
* **frontend:** remove duplicate initHistoryView and gate post-auth dashboard load ([150eaf8](https://github.com/s3ntin3l8/ai-usage-tracker/commit/150eaf848a37f7f1c45f3c66b5337b97a2411a5b))
* **frontend:** use data-value attributes and event delegation for filter pills ([fb226e6](https://github.com/s3ntin3l8/ai-usage-tracker/commit/fb226e6d271aeafbb399fea74f20a14c27b89c0b))
* **gemini:** reverse token priority to avoid picking up sidecar tokens in local mode ([057e50e](https://github.com/s3ntin3l8/ai-usage-tracker/commit/057e50e938a8e97a026c7e9c2f1cb5940509f971))
* **health:** prioritize custom account labels for file-based tokens ([c3c374c](https://github.com/s3ntin3l8/ai-usage-tracker/commit/c3c374ce728f383490a20f5df198cafa059990e2))
* hide zero values in history table ([9120458](https://github.com/s3ntin3l8/ai-usage-tracker/commit/912045885f47ae852e00dd30e4f69130e6aedae7))
* **history:** services filter now applies to chart as well as table ([e353291](https://github.com/s3ntin3l8/ai-usage-tracker/commit/e353291696c380ae3afea90e7879768e0a56f8f3))
* **history:** use epoch-based bucketing so snapshots within a bucket collapse ([10de0ba](https://github.com/s3ntin3l8/ai-usage-tracker/commit/10de0ba74a10b68c725f3648783a428cb715785a))
* improve window classification and account_label display ([84a0c71](https://github.com/s3ntin3l8/ai-usage-tracker/commit/84a0c71529ce14ae28eaa86b27bc43d587e7f099))
* **networking:** allow remote access when APP_HOST=0.0.0.0 ([68671db](https://github.com/s3ntin3l8/ai-usage-tracker/commit/68671db274fb5917d6249f8a87a8b0fbeee711e9))
* **poller:** include service and window identity in dormancy hash ([290af11](https://github.com/s3ntin3l8/ai-usage-tracker/commit/290af11e22a52f1f15165de76f17b2f0b93beb0d))
* **providers:** restore settings UI input fields and fix Ollama data source detection ([5087caa](https://github.com/s3ntin3l8/ai-usage-tracker/commit/5087caae28cac982645157d2e48f284f1f617818))
* **sidecar:** 5 security/correctness bugs from code review ([286d497](https://github.com/s3ntin3l8/ai-usage-tracker/commit/286d4975ecbbefb3d001c3929f2c7c74a9a74d40))
* **sidecar:** 6 bugs + cleanup from code review ([f9d1b58](https://github.com/s3ntin3l8/ai-usage-tracker/commit/f9d1b588afc2b6135d58bc6e6842e3db79b7cabb))
* **sidecar:** add pystray-level diagnostics to tray.run() ([e240bec](https://github.com/s3ntin3l8/ai-usage-tracker/commit/e240bec5ee4553779978dd684804bc81a75a2eb8))
* **sidecar:** add Windows GitHub CLI path for hosts.yml token extraction ([a2106f1](https://github.com/s3ntin3l8/ai-usage-tracker/commit/a2106f13cc3e1c6548d09e19fc40a7fdf7c05f32))
* **sidecar:** address five bugs found in code review ([ccfaf25](https://github.com/s3ntin3l8/ai-usage-tracker/commit/ccfaf25ee4b1eb87f9f3764b555bee33d622a7cd))
* **sidecar:** enable console=True + add startup diagnostics for tray debugging ([f46e38b](https://github.com/s3ntin3l8/ai-usage-tracker/commit/f46e38bf828fae968051cde03a73eb499e82e262))
* **sidecar:** explicitly set icon.visible=True in setup callback ([5c89a4a](https://github.com/s3ntin3l8/ai-usage-tracker/commit/5c89a4a89049e543fcc806d75d8b1e79a52a2c23))
* **sidecar:** fix f-string lint in tray diagnostics ([f530ed3](https://github.com/s3ntin3l8/ai-usage-tracker/commit/f530ed3e697aef1b6532ab2b550556a696da71bf))
* **sidecar:** guard notify() call against Windows notification failures ([8f37cd6](https://github.com/s3ntin3l8/ai-usage-tracker/commit/8f37cd64cc20bc1b5724c667249a09225cc03a90))
* **sidecar:** guard reset_ts parsing, align credit health fallback, use is-None check ([44dba60](https://github.com/s3ntin3l8/ai-usage-tracker/commit/44dba604d35de5add162a04855b4114d1baba7e4))
* **sidecar:** remove unused sys import in __main__.py ([d882cd3](https://github.com/s3ntin3l8/ai-usage-tracker/commit/d882cd358b8864145f18444a3b82b89465bf2de4))
* **sidecar:** use PowerShell for Antigravity LSP process/port detection ([c655699](https://github.com/s3ntin3l8/ai-usage-tracker/commit/c65569971c040988d75bd433407a65d2e9ef3eb1))
* stabilize brittle history test and add pip-audit ([77be7db](https://github.com/s3ntin3l8/ai-usage-tracker/commit/77be7dbd332be7d15fd74b9d997d2361a07ddad9))
* **tests:** give forecast endpoint tests an in-memory DB session ([00727c1](https://github.com/s3ntin3l8/ai-usage-tracker/commit/00727c18d9013e8efa63c462045b0a43120facce))
* **tests:** isolate test_ingest_key_default_detection from .env file ([b106d62](https://github.com/s3ntin3l8/ai-usage-tracker/commit/b106d62a65064378743243df29212b86b58af46b))
* **tests:** prevent flaky multi-day history test from spilling into a 4th day ([d156f20](https://github.com/s3ntin3l8/ai-usage-tracker/commit/d156f20dd22c23dea6a35d77d57c161cc81f82e1))
* **tray:** transparent icon background + larger icon size ([efc2df3](https://github.com/s3ntin3l8/ai-usage-tracker/commit/efc2df353ace3f52fc934012b52b4978540a742d))
* **ui:** remove spurious backtick that terminated buildCard template literal early ([176615c](https://github.com/s3ntin3l8/ai-usage-tracker/commit/176615cc42ff23f6ace87ff0f29fe288fe1f2482))
* update .env file path in dev makefile target ([b146f18](https://github.com/s3ntin3l8/ai-usage-tracker/commit/b146f18ac5af5f4e69ea3a6b4cb0cb0b139b5e0e))
* use bucketed timestamps for grouping to handle slight timing differences ([a1b187e](https://github.com/s3ntin3l8/ai-usage-tracker/commit/a1b187e0186fab87762082123eeb2b696ea751bd))


### Performance Improvements

* **charts:** keep ECharts instance alive across metric/window/peak toggles ([6afb035](https://github.com/s3ntin3l8/ai-usage-tracker/commit/6afb0353c657fbf012288f36f769128c4bb26cf6))
* **frontend:** skip auto-refresh when tab is hidden; catch up on visibility ([4ce6805](https://github.com/s3ntin3l8/ai-usage-tracker/commit/4ce680581ee528be94ad0360f11f4d7f006efac1))
* **history:** scale raw DB fetch limit to requested output size ([5002d26](https://github.com/s3ntin3l8/ai-usage-tracker/commit/5002d2648e4ad381738fe64fa09beeb83b0b9e71))
* **smart-collector:** shallow copy in _tag_as_cached, keep deepcopy for fresh result ([7a9ddf0](https://github.com/s3ntin3l8/ai-usage-tracker/commit/7a9ddf0d83216d4e789703dbcf49f8b7ea75fe22))

## [0.12.2](https://github.com/s3ntin3l8/ai-usage-tracker/compare/v0.12.1...v0.12.2) (2026-04-14)


### Bug Fixes

* **build:** declare sidecar.py stdlib imports as PyInstaller hiddenimports ([401a54f](https://github.com/s3ntin3l8/ai-usage-tracker/commit/401a54f321d162c2c73aed668a6dab40ad981be8))

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
