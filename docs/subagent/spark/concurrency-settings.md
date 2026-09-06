Read /Users/nicholasliao/clawd/DevOps_Practice/references/subagent-system-prompt.md first.
TASK_NAME: model_concurrency_settings
MODE: implement
GOAL: Add user adjustable integer model concurrency 1..8 to Miloco model settings API/profile and web UI.
WORKDIR: /Users/nicholasliao/clawd/xiaomi-miloco/.worktrees/model-concurrency
SCOPE: backend/miloco/src/miloco/config/settings.py; backend/miloco/src/miloco/admin/router.py; backend/miloco/tests/admin/test_omni_config.py; backend/miloco/tests/test_config.py (if present); web/src/components/UsageOmniConfig.tsx; web/src/lib/types.ts; web/src/api/real.ts; web/src/api/index.ts; web/src/i18n/locales/ (model-settings keys only); relevant model-settings tests under web/tests/.
OUT_OF_SCOPE: perception runtime, metrics, deploy, other features.
SOURCE_OF_TRUTH: these existing model config paths and approved user request.
REQUIREMENTS: You are not alone in this codebase; do not revert others' edits. Add model.omni.concurrency default=1, strict integer 1..8, corresponding saved profile field. Read/write/profile save/rename/activate retains concurrency; partial legacy requests preserve existing values, missing old profile defaults 1. Do not alter token masking/auth or trigger probe merely to change concurrency of active profile. Web model edit form labelled 模型并发数 with 1..8 controls, helper explaining simultaneous model requests and cost; active/profile display reflects saved value. Runtime main will consume get_settings().model.omni.concurrency dynamically; do not implement scheduler. Preserve API compatibility. Use TDD: regression fails before implementation, then passes. Do not spawn agents.
ACCEPTANCE_CRITERIA: Save/read/activate roundtrip 8 and 4; invalid 0,9,fraction/bool rejected; legacy clients preserve current profile concurrency; accessible UI input populated and saved.
VERIFICATION_COMMANDS: cd /Users/nicholasliao/clawd/xiaomi-miloco/.worktrees/model-concurrency/backend && PYTHONPATH=miloco/src:miot/src /Users/nicholasliao/clawd/xiaomi-miloco/backend/.venv/bin/python -m pytest miloco/tests/admin/test_omni_config.py -q --tb=short ; cd /Users/nicholasliao/clawd/xiaomi-miloco/.worktrees/model-concurrency/web && npm test -- --run tests/usageOmniConfig.test.ts && npm run build (locate actual existing test filename if different and report exact command).
WRITE_AUTHORITY: scoped file edits only, test/build output paths allowed.
GIT_AUTHORITY: none; do not stage/commit/push.
PRODUCTION_AUTHORITY: none.
DEPENDENCIES_OR_ASSUMPTIONS: Existing backend venv reused with explicit PYTHONPATH; web/node_modules symlink already available.
RETURN_CONTRACT: Write compact report to docs/subagent/spark/concurrency-settings-report.md (allowed) with changed files, RED/GREEN evidence, outstanding issues.
