# LynxLoco upstream merge and deployment result — 2026-09-11

**Completed:** all111 upstream commits merged and published; exact candidate `71dacdcb422e8bde660efe1c4e737b7b6fec5661` deployed to miloco.esxi. **CHG260911006 Successfully Closed.** The known single-task pause was explicitly accepted by the user and is in effect; no task split or rollback was performed.

## Published and deployed source

- Repository: https://github.com/IIIIOvOIIII/lynxloco
- Upstream: XiaoMi/xiaomi-miloco main `75aed171`; all111commits since the previous tracked upstream point are ancestors of the merged candidate.
- Actual deployed source: `71dacdcb422e8bde660efe1c4e737b7b6fec5661`.
- Backend/CLI: `2026.8.6.post1.dev463+g71dacdcb4`; OpenClaw plugin: `2026.8.6-post1.dev463`.
- Native OpenClaw/supervisor runtime and Python3.13 retained. Deployment used the existing native deploy.sh with immutable schema2 artifact/receipt and retain-on-failure.
- Subsequent main commits only publish these records; they are not the deployed source.

## Integration changes

Preserved RTSP, Responses image mode, Home Assistant policies, dashboard authentication and8-way window processing while merging upstream task state, rule action, MIoT scope/property and empty-input changes. Fork application schema v4 already reconciled authentication and token usage, so upstream task migration now applies as fork v5. Existing migrated task actions remain intact on repeated startup and missing-version recovery.

The new task-action endpoint reuses the fork action policy, including cooldown rules, and the HA scene alias is covered by the execution guard. Regressions reproduced the missing policy before the fix. Test fixtures were aligned with the upstream effectively-enabled-rule API and fork media signature; no threshold was weakened.

## Agreed task migration result

Read-only production preflight found one active task with five heterogeneous rules would be paused by the new upstream migration. CHG260911002 was closed Not Executed before any deployment. A split was rehearsed in memory, but the user explicitly chose **direct upgrade** and accepted the pause.

CO006 explicitly authorized that outcome and received human Force Approval. After migration there are **five tasks: four active and one paused**, and **nine retained rules**. The paused group’s five reminders are inactive; they are not counted as working functionality. No task split, manual reminder reactivation or compensating automation was performed. Private task descriptions are omitted from this public report.

## Deployment and preservation evidence

Exact-SHA verify-deploy passed with Implement/active PAM for miloco.esxi/root. Normal native deploy.sh completed successfully, followed by native verification of installed versions, model/plugin files, loaded plugin and gateway RPC, running interpreter and health.

- Application DB v5; observability DB v5; integrity checks passed.
- Miloco configuration bytes, authentication user rows and legacy rule fields preserved.
- OpenClaw agents/channels/models/gateway/plugins settings preserved; only its meta section changed.
- Active/savedGrok4.6 Responses remains concurrency8, timeout180seconds; shared window180seconds. Actual runtime peak concurrency8.
- Dashboard and health reachable over LAN with HTTP200; anonymous protected camera API returns401.
- Home Assistant connected; perception engine running and ready; task APIs include valid new action slots/runtime fields.
- Backup retained: `/opt/miloco-openclaw/backups/71dacdcb422e8bde660efe1c4e737b7b6fec5661`.

## Natural runtime observation

Fixed observation interval: **2026-09-11T08:34:38.429+08:00 → 2026-09-11T08:44:38.429+08:00**,600seconds. No synthetic provider requests, device control commands or test notifications were issued.

| Measure | Observed result |
| --- | --- |
| Health / process | 12healthy samples; PID2456467 stable |
| Enabled RTSP sources | Both connected and advanced frames |
| Successful perception windows | 23and26 across the two sources |
| Recorded model requests / request errors | 50 / 0 |
| Recorded cycles / skipped cycles | 224 / 172 |
| Dropped windows | 80;26.32% using dropped/(cycles+dropped) |
| Actual peak concurrency | 8 |
| RSS peak / final | 2532.9 / 2459.0 MiB |
| Minimum available memory | 2366.5 MiB |

The zero request-error counter and per-camera successful-window counts are distinct measurements; this report does not equate50requests to50successful camera windows. Queue drops remain a capacity limitation. This bounded observation does not establish all-day reliability or notification delivery, and the user-accepted paused reminders remain inactive.

## Local qualification

| Scope | Result |
| --- | --- |
| Backend (including auth, camera, HA, migration and4/8-way concurrency) | 4995passed,1skipped |
| CLI | 680passed |
| Web | 525passed,1skipped; build passed |
| OpenClaw plugin | 191passed; typecheck/build passed |
| Changed MIoT subscription units | 75passed |
| Native/Docker deployment, scripts and Hermes | 428passed,2skipped |
| Changed Python / immutable native build | Lint passed / build passed |

Backend e2e/agent directories and live SDK integrations requiring external credentials were outside local unit qualification. Production validation above supplies the scoped runtime evidence. Existing warnings and a transient initial model-load timing failure were recorded; the complete final backend run passed without reducing thresholds.

CO006 was closed Successfully Closed only after deployment and the agreed observation/acceptance completed. No further production access is authorized under closedCO002orCO006. [Progress](2026-09-11-upstream-main-merge_PROGRESS.md) retains the chronology and local receipt pointers.
