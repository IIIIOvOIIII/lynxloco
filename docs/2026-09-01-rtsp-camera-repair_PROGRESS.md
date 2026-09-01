## 2026-09-01 00:43 SGT

- Current work: Diagnose the user-reported production RTSP camera failures on `miloco.esxi`: kitchen reports `RTSP video codec could not be decoded`, and large-balcony reports `RTSP authentication failed`.
- Expected result: Establish whether each failure is caused by Miloco code/runtime state, persisted RTSP configuration shape, network/source behavior, or invalid source credentials, without exposing RTSP URLs, usernames, passwords, tokens, model keys, or camera frames.
- Result: In progress. Local source history shows current `main` contains the prior RTSP and Omni repair commits, so the first hypothesis is not a whole-branch rollback of the earlier RTSP fixes.
- Next step: Open a narrowly scoped production CO for sanitized RTSP verification and bounded repair on `miloco.esxi`.

## 2026-09-01 00:46 SGT

- Current work: Executed production RTSP diagnosis and bounded repair under CO `CHG260901001`.
- Expected result: Verify the affected kitchen and large-balcony RTSP sources with sanitized evidence, clear any Miloco-owned stale runtime state, and preserve all persisted Miloco configuration and credentials.
- Result: Achieved. Production `/health` checks for Miloco on `1810` and OpenClaw on `18789` returned HTTP 200. Sanitized configuration inspection found both affected RTSP sources enabled; the kitchen source had separate saved credentials while the large-balcony source had no saved username/password and no URI userinfo. Direct in-process Miloco RTSP probes using the saved configuration succeeded for both sources: kitchen decoded H.264 at 2560x1440 with PCM_ALAW audio, and large-balcony decoded H.264 at 1920x1080 without audio. The dashboard/API errors were therefore stale terminal runtime state rather than current source reachability failure. Calling the existing enabled-camera retry path for both sources cleared the runtime errors without changing persisted configuration. Eight follow-up polls over roughly 40 seconds showed both sources connected, frame timestamps advancing, and `error_code=null`. Final health checks showed OpenClaw plugin `miloco-openclaw-plugin` loaded at `2026.8.6-post1.dev259`.
- Next step: Close CO `CHG260901001`. If this stale-terminal pattern recurs frequently, consider a separate code change to surface a clearer retry affordance or perform bounded background revalidation for terminal RTSP states, while preserving the no-infinite-auth-retry safety behavior.

## 2026-09-01 00:56 SGT

- Current work: Design the productized RTSP terminal-state auto-recovery requested after the production stale-state repair.
- Expected result: Capture a narrow written design before implementation: RTSP-only, retry every 10 minutes for 12 hours, recover on success, and auto-disable the RTSP source if the window expires.
- Result: Achieved in draft. Specification added at `docs/superpowers/specs/2026-09-01-rtsp-auto-recovery-design.md`. The design keeps recovery in the backend RTSP runtime, reuses the existing retry path, keeps the failure timer bounded so genuinely bad credentials are not retried forever, and preserves all non-RTSP state.
- Next step: Self-review the spec, commit it, then proceed to implementation planning once the written spec is accepted.

## 2026-09-01 12:53 SGT

- Current work: Implemented the approved RTSP-only bounded auto-recovery behavior in an isolated worktree branch.
- Expected result: Terminal RTSP sources are retried no earlier than 10 minutes after failure, keep their original 12-hour recovery deadline across repeated terminal failures, recover automatically when a retry reconnects, and only the matching RTSP source is disabled if the 12-hour window expires.
- Result: Achieved locally at implementation commit `d8e48d1b490f5e2db60bbc66f6bf610d8bfbda5e`. Added source-level recovery state, a periodic adapter advance hook, and tests for retry timing, expiration auto-disable, repeated-failure deadline preservation, config-change reset, same-cycle adapter retry, and MIoT non-impact. Verification passed: `uv run ruff check ...`, `uv run ty check ...`, and `uv run --package miloco pytest miloco/tests/perception/collect/test_rtsp_camera_source.py miloco/tests/perception/collect/test_camera_adapter.py -q` with 38 passed.
- Next step: Commit the progress record, merge the implementation branch back to `main`, push, then create a production Software CO for deployment to `miloco.esxi` using the official installer/sync path while preserving `/root/.openclaw/miloco`.
