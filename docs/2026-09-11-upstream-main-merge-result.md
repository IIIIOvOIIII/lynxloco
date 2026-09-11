# LynxLoco upstream merge — release prepared, deployment awaiting approval

The upstream integration, publication and immutable release are complete. Deployment is blocked by a live-data compatibility issue found after user approval and before installation. CHG260911002 is closed Not Executed; production is unchanged.

## Published source

- Repository: https://github.com/IIIIOvOIIII/lynxloco
- Merge candidate: `71dacdcb422e8bde660efe1c4e737b7b6fec5661`
- Upstream: XiaoMi/xiaomi-miloco main `75aed171`, all 111 commits since the previous tracked upstream point. Git ancestry and remote main SHA verified.
- Backend/CLI: `2026.8.6.post1.dev463+g71dacdcb4`; OpenClaw plugin: `2026.8.6-post1.dev463`.
- This report and progress updates are documentation only and do not change the deployment candidate.

## Integration decisions

- Retain RTSP, Responses image mode, Home Assistant control policy, dashboard authentication and 8-way concurrent window processing.
- Preserve fork schema v4 authentication/token-usage reconciliation; migrate upstream task runtime fields as fork application schema v5. Already-migrated task actions are retained on repeated startup and missing-version recovery.
- Apply the fork action validator to the new task action endpoint, and include the HA scene alias in the execution cooldown guard. Tests reproduced the policy bypass before the fix.
- Adapt upstream test calls to fork media arguments and the concurrency fixture to the new effectively-enabled-rule API. No test threshold was weakened.

## Validation

| Scope | Result |
| --- | --- |
| Backend, including database migrations, auth, camera, HA and 4/8-way concurrency | 4995 passed, 1 skipped |
| CLI | 680 passed |
| Web | 525 passed, 1 skipped; production build passed |
| OpenClaw plugin | 191 passed; typecheck and build passed |
| Changed MIoT subscription units | 75 passed |
| Native/Docker deployment, scripts and Hermes | 428 passed, 2 skipped |
| Changed Python files | Lint passed |
| Native deploy.sh build | Passed, exact-SHA payload and receipt created |
| Independent merge review | No remaining blocking findings |

Backend e2e/agent directories require a running environment and are excluded from local qualification. Live SDK integration tests requiring unavailable external credentials were stopped; changed MIoT subscription units passed separately. Existing warning output and one transient model-load timing failure were observed; the complete final backend run passed without changing thresholds.

## Approved preflight and deployment boundary

User approval was verified as Force Approved, Implement and active PAM. Exact-SHA verify-deploy and native preflight passed. Current source was freshly verified as cb87f6c0/dev350, health200, active/saved8concurrency and180timeout, application schema4 and observability schema5. Two enabled cameras were connected. No deployment or restart was performed.

Live data contained five active tasks and nine rules. One task grouped five heterogeneous rules with different actions. The new upstream task migration would pause it and leave those five reminders inactive. A read-only SQLite snapshot copied into memory and migrated with the exact candidate functions confirmed the behavior.

A proposed alternative was rehearsed in memory: split the affected group into five independent tasks, retain its original task as paused history, and preserve all rule IDs, conditions, enabled flags, actions and authentication data. The result was nine active tasks plus one retained paused task, nine rules, matching migrated actions, quick_check ok and zero foreign-key violations. The affected task had no cron/progress/duration/event records requiring redistribution. This proves the data transformation; runtime/notification acceptance remains unmeasured.

CHG260911002 was closed **Not Executed** after these read-only checks. There were no production database/configuration changes, installs, restarts, device actions or rollbacks. The proposed task data adjustment requires user confirmation and a revised CO scope before proceeding. Private task details and the proposal remain in the ignored CO directory and are not published in this repository. See [progress](2026-09-11-upstream-main-merge_PROGRESS.md) for continuation pointers.
