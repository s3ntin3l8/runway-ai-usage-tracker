# Physical Windows validation — 12 October 2026

Evidence for #597 and #603, on Windows 11 Pro x64, build 26200. These results do not close either issue.

## Defects reproduced and fixed

- Windows `CreateProcess` failed with WinError 2 when `cmd.exe` was supplied as a relative explicit application name. Resolve the system command processor absolutely; the real native batch-helper regression swaps files from a directory containing spaces.
- Windows `SO_REUSEADDR` allowed the test tray and installed tray to share port 17653. Settings/status requests could reach the wrong instance. Use exclusive address ownership on Windows and retain Unix address reuse; independent instances now select different ports and a reuse-enabled contender cannot bind the owner's port.
- A successful update left `self-update.lock` behind because `_release_lock` unlinked an open CRT descriptor before `os._exit`. Close the owned handle before unlinking, protect lease identity against descriptor reuse, and preserve another updater's lock.

## Packaged memory comparison

Baseline 510eb785 versus optimization 2e58912a, identical Python 3.12.14 / PyInstaller 6.22.3 and installed dependencies. Three alternating runs per revision. Each run uses a clean child environment, disposable profile/config/history, synthetic Claude credentials, and a local mock server. Launcher and worker are sampled separately every 100 ms.

Both packaged revisions include the same test-only runtime hook forcing exclusive ownership of settings ports 17653–17672. No collection/model/queue code is changed by that hook. This instrumentation is necessary to prevent the released Windows socket-sharing defect from routing test log/settings requests to the installed sidecar. The raw reports retain both binary hashes and the hook hash. Earlier runs with ambiguous settings ownership are excluded.

Working set is resident process memory; private bytes are committed private process memory. Peak working set is the process-lifetime high-water mark, so later phases inherit earlier peaks. Values below are medians of run medians (MiB); backfill peak uses the median of each run's native high-water mark. Task Manager's memory column is not used.

| Worker phase | Working set baseline → optimized | Private bytes baseline → optimized |
| --- | --- | --- |
| startup | 38.375 → 41.312 | 25.059 → 27.867 |
| warmed_idle | 70.711 → 69.898 | 49.684 → 48.961 |
| backfill | 88.406 → 87.918 | 67.820 → 67.238 |
| repeated_polls | 77.227 → 77.084 | 56.152 → 55.816 |
| log_viewing | 78.219 → 77.176 | 56.379 → 55.469 |
| offline_queue | 78.289 → 77.184 | 56.332 → 55.730 |
| queue_replay | 78.242 → 77.258 | 56.238 → 55.719 |

Backfill peak working set: **111.078 → 103.887 MiB**. Persisted-watermark completion: **44.985 → 41.625 seconds**. The timer includes the next heartbeat and disk writes, rather than merely stopping when the server receives the batch. Host activity varied; this is not a controlled speedup claim.

All six runs delivered the same 10,100 unique event payloads/digest (10,000 backfill + 100 subsequent events), created and drained the offline queue, verified settings ownership, reloaded a changed synthetic key through the actual settings callback, and reported the server-synchronized beta channel. Log viewing requests 200-line tails twenty times from a 50,000-line fixture. The warmed-idle plateau remains: the data supports lower workload peaks, not a general 65 MB idle reduction.

The native Windows queue-sharing regression holds a real destination handle without FILE_SHARE_DELETE: failed replacement retains the complete original queue; closing that external handle allows retry and deletion. Other focused checks cover serialization/order, retry/watermark behavior, fdopen failures and handle closure.

The separate seven-case source benchmark ran three times both untraced and with tracemalloc. Native output parity passed against the exact optimization revision. Comparing today's main to the old baseline instead fails the backfill digest, so that comparison is not presented as optimization-only parity.

## Signed installer and native update sequence

Published beta.8 ZIP/setup and captured edge ZIP/setup verify against the repository's main signing workflow and GitHub issuer. Captures record release IDs, asset IDs, sizes, update times and SHA-256, with unchanged metadata checked before/after downloading. The rolling edge bytes are stored as an immutable local asset set; the executable embeds 3.0.0-beta.9+edge.70779c4.

The signed beta.8 setup ran silently into a workspace test directory, returned 0, installed the exact portable executable, created the expected Start Menu links and registered version 3.0.0-beta.8. Existing per-user uninstall/protocol registrations and the two Start Menu shortcuts were snapshotted and restored exactly. The user's actual installation was not replaced.

Both portable and installer test paths completed A launch → signed B swap/restart → A rollback/relaunch → B swap/restart again. Configuration hashes remained unchanged; backup hashes matched A/B; swaps waited for the mapped old image to exit; locks were released. Frozen diagnostics accepted the signed archive and rejected tampered bytes, a missing bundle, and a malformed bundle; A relaunched afterwards with its original hash.

**Initiation limitation:** the Windows update/rollback driver runs the corrected source updater, substitutes captured release metadata/version discovery and a loopback asset server, and targets unmodified signed frozen executables. Production checksum, Sigstore verification, extraction, batch helper and frozen relaunch remain active. Version evidence is embedded package.json plus control-file PID/executable ownership, not an ambiguous HTTP status response. This is native swap/relaunch integration evidence; it is not proof that a newly signed release containing these fixes works through the original tray/published-channel path.

The separate production refusal probe passed missing bundle, malformed bundle, tampered payload and oversized response with installed/rollback copies unchanged, lock acquisition observed and final release confirmed.

## Checks and remaining acceptance

Focused Windows regressions: 161 passed, one POSIX-only test skipped, including real socket binding, real batch execution, hard process exit, and native locked queue replacement. Ruff, mypy and the dependency audit pass. Full host gate remains red independently: initial full pytest 4,184 passed / 122 failed / 173 errors / 45 skipped, including SQLite fixture WinError 32 cleanup. Frontend initially had 1,141 passed / 14 failed with locale and pnpm layout failures; with npm-compatible hoisting it has 1,149 passed / six locale-dependent assertions failed. Do not report a green full local gate.

Still required: publish a signed build containing the fixes, then execute the genuine tray/published-channel update/rollback and tray-control/Start Menu UI checks; physical hostile-origin/LNA/DNS tests; macOS acceptance. [#621](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/621) tracks full native Windows gate portability.

Sanitized machine-readable artifacts: [packaged memory](evidence/windows-2026-10-12/packaged-memory.json), [native matrix](evidence/windows-2026-10-12/native-matrix.json), [installer](evidence/windows-2026-10-12/native-installer-report.json), [refusal probe](evidence/windows-2026-10-12/windows-update-refusal-admin.json), [dependencies](evidence/windows-2026-10-12/dependencies.txt). Raw logs, child profiles, registry backups and control tokens remain local and are excluded from publication.
