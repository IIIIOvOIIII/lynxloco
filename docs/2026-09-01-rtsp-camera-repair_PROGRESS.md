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
