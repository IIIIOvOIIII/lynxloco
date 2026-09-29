# LynxLoco native operations

Production uses the existing native OpenClaw/supervisor profile on miloco.esxi, not the Docker template. Keep its release/backup controller and the user's retain-on-failure policy.

## Local qualification

Install locked dependencies with `uv sync --frozen` in backend and CLI, and `pnpm install --frozen-lockfile` in web and plugins/openclaw. The shared v4 runner is loaded from `~/clawd/DevOps_Practice/tools/quality_gate`; the backend environment supplies its Python dependencies.

Run `scripts/run_quality_gate.sh --gate deploy-preflight` before any deployment SSH. After it passes, move the generated `backend/.coverage` into `.tmp/coverage/backend.data` before native clean-worktree checks; retain the XML and gate summary as evidence. It runs backend, CLI, web, plugin, lint, web build and native/Docker/script/Hermes contracts. Backend coverage measures only production source (not test code); timed model-load/concurrency tests run in a separate process with their assertions unchanged, appending their real coverage to the functional suite.

This upstream fork's existing local suite measures about81.56% of backend production lines and16.09% of all frontend source/public-JS lines. The v4 config records81%/16% floors, rather than adding unrelated tests or claiming the standard85%/80% defaults were met. Frontend tests primarily cover pure functions, APIs and source contracts, not rendered component behavior. Branch coverage is reporting-only. New functionality should improve these baselines.

## Release

1. Publish the tested exact commit to origin/main. From its clean checkout run `MILOCO_DEPLOY_RUNTIME=openclaw ./deploy.sh build` to create immutable native artifacts, including the matching MIoT native libraries and OpenClaw plugin.
2. Create a Software CO with that SHA, miloco.esxi/root and the concrete native deployment plan. A denied CO stays open and unchanged for Lynx's decision. Never reuse a closed CO/PAM grant.
3. Immediately before SSH require fresh Implement/activePAM using itsm-co. The existing exact-SHA payload/receipt verifier can also be used.
4. With `MILOCO_DEPLOY_RUNTIME=openclaw`, `MILOCO_DEPLOY_PRODUCTION_HOST=miloco.esxi`, `MILOCO_SSH_IDENTITY=~/.ssh/id_co_openclaw` expanded to an absolute path, and `MILOCO_OPENCLAW_FAILURE_POLICY=retain`, run the repository `deploy.sh preflight`, `deploy`, then `verify` with miloco.esxi as target.
5. Run `scripts/run_quality_gate.sh --gate post-deploy`. Supplement the public HTTP smoke with approved read-only native version/config/task/camera checks and a10minute natural camera observation. No synthetic model requests or device actions.

Backups remain at `/opt/miloco-openclaw/backups/<release SHA>` and include online SQLite snapshots, native environments, plugin/models and configurations. Do not restore/delete them or roll back automatically. After explicit user authorization and valid CO/PAM, native `deploy.sh rollback miloco.esxi <transaction SHA>` can restore its transaction. Application-data recovery needs its own concrete review because code rollback does not reverse migrated task metadata. Close the CO according to the actual result.
