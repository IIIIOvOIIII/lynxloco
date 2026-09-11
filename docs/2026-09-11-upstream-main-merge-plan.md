# LynxLoco upstream merge implementation plan

**Goal:** Merge all 111 upstream commits through 75aed171 into LynxLoco main and update the existing miloco.esxi native deployment under an exact-source CO.
**Architecture:** Merge upstream history while retaining fork RTSP, Responses image mode, Home Assistant actions, dashboard authentication and concurrent window processing. Preserve fork schema v4 reconciliation and apply upstream task runtime migration as v5.
**Tech Stack:** Python/uv, React/TypeScript/pnpm, SQLite, native OpenClaw/supervisor.
**Spec:** User request in this task and existing deployment contract in docs/2026-09-08-upstream-main-merge-plan.md.

## Constraints and budget

- Keep unrelated .codex_tmp and all other worktrees intact. Use .worktrees/upstream-main-20260911.
- Preserve active/saved concurrency 8, timeout/shared window 180, model/provider and all user configuration.
- Retain on deployment failure; no automatic rollback or four-way fallback. Any restore requires later user authorization.
- No production SSH before Implement/active PAM. No device commands, synthetic model load or notification tests.
- Budget: 120 minutes active work, two local repair cycles per failure class (absolute maximum five), one candidate deployment and ten-minute natural observation. Stop on CO human approval/denial or a material unresolved migration problem.

## Tasks

- [x] Merge upstream and resolve six conflicting files. Keep both imports and methods in admin/rule code; combine empty-window filtering with fork concurrency wrapper and image payload mode.
- [x] Reconcile connector migrations: fork v3-v4 remains; upstream task schema becomes v5, already-migrated shapes retain task actions, missing-version detection does not skip auth reconciliation. Verify legacy fork v4 upgrade, idempotence and upstream migration suite.
- [x] Run backend/MIoT, CLI, web, OpenClaw and native deployment regression suites, changed Python lint, and build using MILOCO_DEPLOY_RUNTIME=openclaw ./deploy.sh build. Review fork customizations and generated artifacts.
- [x] Commit and push candidate to origin/main using existing GitHub credential helper over HTTPS; bind exact SHA, artifacts, host and native deploy command to Software CO.
- [x] After Implement/active PAM and verify-deploy, inspect existing deployment read-only, check configuration and database shape, deploy via deploy.sh with retain policy, verify version/assets/auth and ten minutes of natural camera progress.
- [x] Record actual outcome and limitations, close CO truthfully, publish progress/closeout, update memory note.

Current result: source and immutable dev463 release complete. CHG260911002 was manually approved; read-only preflight found an existing task would be paused by upstream migration. It is now closed Not Executed, with no production mutations. A compatible task split was rehearsed only in memory and awaits user confirmation and revised CO scope. See the progress document.

Superseding decision: user approved direct upgrade and accepted the known single-task pause. No split will run. CHG260911006 awaits Lynx approval in ITSM; use its new upstream-direct payload/receipt for the existing71dacdcb candidate.

Final outcome: user-approved direct upgrade deployed71dacdcb/dev463 through CO006, all scoped acceptance passed, CO Successfully Closed. Known single-task pause is accepted and remains in effect. No splitting or rollback. See final result/progress for evidence.
