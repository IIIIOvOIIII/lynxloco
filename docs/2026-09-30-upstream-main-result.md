# LynxLoco upstream integration and deployment — 2026-09-30

**Completed:** merged all51new upstream commits, published the final source to LynxLoco main and deployed the existing native service. **CHG260930001 Successfully Closed** after scoped runtime acceptance.

## Source and version

- Upstream: `XiaoMi/xiaomi-miloco:main` at `cad239dca9b7a2dd3bf0e6565a26cf9eef6581b8`.
- Merge commit: `af387a0f4ed7a178afb24fc0974cad2df1bba590`; final deployed source: `cf5f9dae4a25d3b35138d2f938acbad1a319d78a` (includes a reproducible coverage-runner correction).
- Remote: https://github.com/IIIIOvOIIII/lynxloco, main publication and ancestry verified before deployment.
- Backend/CLI: `2026.9.11.post1.dev322+gcf5f9dae4`; OpenClaw plugin: `2026.9.11-post1.dev322`.
- Main may advance with documentation-only closeout commits. Actual runtime remains the source SHA above.

## Integrated behavior

Upstream adds IoT property condition/guard handling, fixes camera cross-subnet/native connection and silent-stream recovery, bounds live WebSocket backpressure, and updates activity/task display. Six conflicts were resolved while retaining the fork’s RTSP sources, raw H264/JPEG live view, dashboard authentication, Responses media mode, Home Assistant action validation and8/180concurrent processing.

The MIoT silent-stream watchdog is limited to its own source driver and does not consume RTSP frame-admission clocks or replace RTSP recovery. Existing application schema5 is retained; this release introduces no new schema migration step. The new Linuxx86_64 camera library was checked in the final wheel and on the server against tracked source: `fde805072a663d975da26e1b91836acad2da9c9e19038e382ad4118871c65ecc`.

## Qualification

Final exact-source v4 deploy-preflight passed backend/frontend/CLI/plugin/lint/build/deployment-contract checks. Backend source coverage excludes test files. Timed native/concurrency tests run in their own process with unchanged assertions and append measured coverage. Separate MIoT SDK unit selection passed without using live SDK credentials.

| Check | Result |
| --- | --- |
| Backend functional suite | 5225passed,1skipped; timing test intentionally isolated |
| Isolated model/concurrency tests | 33passed |
| CLI | 715passed |
| Frontend | 559passed,1skipped; production build passed |
| OpenClaw plugin | 191passed; typecheck/build passed |
| MIoT SDK unit tests | 156passed |
| Native/Docker deployment, scripts and Hermes | 428passed,2skipped |
| Backend production-source line coverage | 82.10% |
| Whole frontend source/public-JS line coverage | 16.09% |

The legacy fork suite has project floors81%/16%, documented under the v4 standard’s threshold-adjustment allowance. These results do not meet the standard’s default85%/80% values and are not represented as doing so. Frontend tests primarily exercise pure functions, APIs and source contracts; rendered browser coverage remains limited. Two independent bounded reviews found no remaining blocking integration issue.

## Deployment and preservation

The first HTTPS push was rejected because the local OAuth credential lacked workflow scope, and the original SSH authorization was unavailable. The existing Vault GitHub token successfully published the same source; no account credential configuration or upstream workflow content was removed to bypass the restriction. Source was verified on remote main before production mutation.

CO001 was AI accepted with active PAM for miloco.esxi/root. Fresh live approval and exact payload receipt checks passed, followed by native deploy.sh preflight/backup/deploy/verify with retain-on-failure. A generated local `.coverage` file initially tripped the clean-worktree preflight and was moved to ignored artifact storage before retry; no server mutation occurred in that failed local preflight.

- Runtime remains native OpenClaw/supervisor and Python3.13.
- Current Miloco configuration bytes, authentication user rows, legacy rule fields and task statuses preserved.
- OpenClaw agents/channels/models/gateway/plugins settings preserved; only metadata changed.
- Active/savedGrok4.6Responses remains8concurrency/180secondtimeout, sharedwindow180; new saved model profiles present before release were retained.
- Five tasks remain four active and one previously paused; all nine rules retained. This release did not pause, split or reactivate any task.
- Application/observability schemas remain5, SQLite integrity and foreign-key checks passed.
- Models/plugin files, loaded plugin/gateway RPC and the new installed native camera library verified.
- LAN health/dashboard200; anonymous protected camera API401; Home Assistant connected and perception engine ready.
- Backup retained: `/opt/miloco-openclaw/backups/cf5f9dae4a25d3b35138d2f938acbad1a319d78a`.

## Ten-minute natural observation

Fixed interval **2026-09-30T01:12:30.899+08:00 → 2026-09-30T01:22:30.899+08:00**. No synthetic provider load, test notifications or manual device actions.

| Measure | Result |
| --- | --- |
| Health / process | 14healthy samples, PID1467329stable |
| Enabled RTSP sources | Both connected and advanced frames |
| Successful perception windows | 52and45 |
| Recorded model requests / errors | 97 / 0 |
| Recorded cycles / skipped | 198 / 98 |
| Dropped windows | 76;27.74%using dropped/(cycles+dropped) |
| Actual peak concurrency | 8 |
| RSS peak/final | 2194.8MiB |
| Minimum available memory | 2690.9MiB |

Queue drops remain a capacity limitation. This fixed observation does not establish all-day reliability or notification delivery; the previously paused task remains inactive. Disabled camera sources were preserved, not enabled for testing.

All scoped acceptance checks passed and CO001 was closed Successfully Closed. No further production access is authorized by that closed CO. [Archived progress](archive/2026-09-30-upstream-main_PROGRESS.md) records execution and private receipt locations; [operations](OPERATIONS.md) describes the native workflow.
