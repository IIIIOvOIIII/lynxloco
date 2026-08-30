# Perception Runtime Transparency Progress

## 2026-08-30 17:22 +08

- Current work: Converted the approved first and second phase findings into a dedicated implementation plan in a separate Miloco worktree.
- Expected result: A new worktree/branch exists for follow-up development, with a written design and task-by-task implementation plan covering runtime transparency, semantic-empty diagnostics, realtime caption repair, verification, and later production rollout boundaries.
- Result: Achieved for setup and planning. Worktree `/Users/nicholasliao/clawd/xiaomi-miloco/.worktrees/perception-log-observability` was created on branch `feature/perception-log-observability`; design and plan documents were added under `docs/superpowers/`.
- Next step: Execute the plan task-by-task from the dedicated worktree, preferably with `superpowers:subagent-driven-development` after explicit execution approval.

## 2026-08-30 17:35 +08

- Current work: Ran a production read-only, one-frame living-room RTSP to Omni endpoint verification before entering worktree development.
- Expected result: Determine whether the current configured Omni LLM endpoint is actually participating in inference, and identify the first failing boundary if not.
- Result: Achieved. Read-only CO `CHG260830029` was approved and closed. `miloco.esxi` reported the living-room RTSP source enabled and connected, with the perception engine running and ready. A real living-room frame was acquired with shape `[2160, 3840, 3]`. A bounded non-fused OpenAI Responses visual request using the current configured endpoint/model/key returned HTTP 200 in 14.366s with usage tokens and response text, proving the LLM endpoint is reachable and participating; Miloco structured parsing still marked the response skipped because the returned text was not valid structured perception JSON. A follow-up single-frame request in production fused-shape form included one image block but timed out at the configured 30s timeout. No code, service, config, database, credential, or deployment mutation was performed; no RTSP URL, API key, frame, prompt, or raw model response was retained.
- Next step: Keep the implementation plan's Phase 1 runtime-summary/dedup visibility and Phase 2 realtime/fused semantic diagnostics. Add an explicit acceptance path for fused-shape timeout versus non-JSON response so the next code change targets prompt/response-contract/timeout behavior rather than treating the issue as a disconnected endpoint.

## 2026-08-30 17:43 +08

- Current work: Updated the realtime implementation plan after the user requested a 120-second model timeout and structured JSON output requirement.
- Expected result: The design and task plan make the new realtime/fused timeout and JSON-only model contract explicit before any worktree implementation starts.
- Result: Achieved. The plan now requires effective realtime/fused Omni calls to use a 120-second model timeout, including default `OmniConfig`, `model.omni.timeout`, settings propagation, live config refresh, and fused HTTP client cache rebuild when timeout changes. The plan and spec now require OpenAI Responses visual/image-sequence calls to return one parseable structured JSON object matching Miloco's active route schema; prose outside JSON remains a parse/diagnostic failure rather than a successful perception result.
- Next step: Execute the updated Task 7 after Phase 1 diagnostic visibility is in place, then use the runtime diagnostics to verify whether 120 seconds plus JSON-only contract produces usable realtime captions on real RTSP frames.

## 2026-08-30 18:11 +08

- Current work: Implemented the approved first and second phase changes directly on `main`.
- Expected result: Miloco exposes enough runtime evidence to distinguish "service running", "raw perception logs being created", "meaningful events being generated", and "Omni returned parseable semantic content"; realtime/fused Omni calls use a 120-second timeout and ask for one structured JSON object.
- Result: Achieved locally. Added backend runtime summary and sanitized Omni diagnostics, a frontend perception runtime card, `model.omni.timeout` propagation, fused HTTP client rebuild on timeout changes, and stricter JSON-only/caption prompt contract. Verification passed: backend targeted perception/settings suite `246 passed, 1 warning`; frontend targeted suite `36 passed`; frontend production build succeeded with only the existing large-chunk warning.
- Next step: Commit the main-branch implementation, open an ITSM CO/PAM for `miloco.esxi`, deploy the exact commit, then validate production health and runtime-summary evidence without exposing secrets or raw camera/model data.

## 2026-08-30 18:22 +08

- Current work: Deployed the committed main implementation to production `miloco.esxi`.
- Expected result: Production runs the exact implementation SHA, keeps Miloco/OpenClaw healthy, exposes `/api/perception/runtime-summary`, and reports the effective Omni timeout as 120 seconds without exposing secrets or raw camera/model data.
- Result: Achieved for deployment and observability. CO `CHG260830030` was approved in `Implement` with active PAM and closed `Successfully Closed`. Exact-SHA gate passed for source `0ed57bebd763b002cfadfecb66bb4ebd28c19609`. The official installer flow deployed version `2026.8.6.post1.dev219+g0ed57bebd`; Miloco local/LAN health returned OK; dashboard root returned HTTP 200 HTML; runtime-summary returned HTTP 200; `model.omni.timeout=120.0` and `perception.engine.omni.timeout=120.0`; OpenClaw gateway stayed healthy and `miloco-openclaw-plugin` loaded at `2026.8.6-post1.dev219`. The new runtime evidence shows one active source and new raw perception rows after restart, but meaningful events remain zero and latest Omni classification is `semantic_empty`.
- Next step: Treat production as healthy but not yet semantically productive: use the new diagnostics for a follow-up targeted fix of realtime RTSP semantic-empty output if the dashboard continues to show no meaningful perception logs after enough camera activity.

## 2026-08-30 19:10 +08

- Current work: Diagnosed the latest production model diagnostic where structured results were empty.
- Expected result: Determine whether `semantic_empty` is an expected no-event state or a defect, then apply the smallest safe repair if it is not expected.
- Result: Partial, local fix ready. Read-only CO `CHG260830034` was approved and closed. Production evidence showed Miloco and the Omni endpoint were reachable, recent Omni calls completed without parser errors, and new raw perception rows were written, but recent `perception_log.descriptions` remained `{}` and `meaningful_events` remained zero. Code review found the OpenAI Responses image-sequence route sets `SceneDescriptor.has_audio=False`; the prompt builder then removed all examples, leaving a schema without a pure-visual JSON example. A TDD fix now preserves pure visual caption/identity examples for no-audio video prompts while still excluding `speeches` and `env_sounds`. Verification passed locally: `TestExamplesGatedByAudio` 5 passed, related Responses/prompt regression suite 77 passed, full prompt builder suite 149 passed, and targeted ruff checks passed.
- Next step: Commit the fix, open a production deployment CO for `miloco.esxi`, deploy the exact commit, and validate that subsequent realtime Omni diagnostics are no longer `semantic_empty`.

## 2026-08-30 19:38 +08

- Current work: Re-diagnosed production after deploying the pure-visual example fix because realtime structured results still stayed empty.
- Expected result: Prove whether the remaining empty result is an expected no-event state, a model endpoint issue, or a realtime fused request-contract issue.
- Result: Achieved for root-cause isolation. Deployment CO `CHG260830036` delivered source `ee037668974a319ce5459c33e27ba7980e42ab4c` and left Miloco/OpenClaw healthy, but post-deploy acceptance still showed consecutive realtime empty descriptions. A production single-variable probe using the official Miloco runtime, the active OpenAI Responses endpoint, and live RTSP JPEG frames reproduced the failure with the unmodified fused request: HTTP 200, 3 input images, but output was not valid JSON and realtime diagnostics remained `semantic_empty`. The same request with only a final JSON/caption guard appended after the images returned valid JSON and one parsed caption. Therefore the remaining issue is not expected silence and not a disconnected endpoint; the fused Responses image-sequence prompt needed an end-of-message output guard because the final user block was an image. A TDD regression now requires the guard to appear after the image blocks. Local verification passed: the RED test failed on the missing guard, then the focused test, adjacent image-sequence tests, `TestExamplesGatedByAudio`, the full Responses/Prompt suite (`210 passed`), and scoped Ruff all passed after the fix.
- Next step: Commit this narrower fix, create a new exact-source production deployment CO for `miloco.esxi`, deploy, and validate that realtime diagnostics and `perception_log.descriptions` produce non-empty structured output without exposing camera imagery or model text.

## 2026-08-30 19:45 +08

- Current work: Deployed and validated the narrower Responses image JSON guard fix on production `miloco.esxi`.
- Expected result: Production should run the exact fixed source, keep Miloco/OpenClaw healthy, and turn the previous realtime `semantic_empty` / structured-empty state into parseable non-empty structured descriptions.
- Result: Achieved. CO `CHG260830037` was approved, implemented, and closed `Successfully Closed`. Exact-source deployment used commit `f83d500c3acdfcc3465d6b2148b112e656a206e8`, version `2026.8.6.post1.dev224+gf83d500c3`, and OpenClaw plugin `2026.8.6-post1.dev224`. Production health checks passed on Miloco port `1810` and OpenClaw port `18789`. The installed prompt builder contains the terminal image JSON/caption guard. A production fused OpenAI Responses probe using three live RTSP JPEG frames returned HTTP 200 with valid structured JSON and one parsed caption. Runtime summary now reports engine ready/running, two active sources, `last_descriptions_empty=false`, `consecutive_empty_descriptions=0`, semantic state `describing`, and five of the latest six perception rows contain non-empty descriptions. No RTSP URLs, API keys, raw camera frames, raw prompts, or raw model text were retained.
- Next step: Leave production running on the fixed version. Treat `meaningful_events=0` as expected until configured rules/suggestions/speech/environment-sound outputs are generated; captions are now present in `perception_log.descriptions`.

## 2026-08-30 20:22 +08

- Current work: Repaired the model-add / model-test OpenAI Responses visual preflight timeout after the UI reported `无法连接 Base URL` and the model backend reported `context canceled`.
- Expected result: Adding or testing a Responses-compatible vision model should keep each visual/structured `/responses` probe open for 120 seconds while retaining the short 10-second connection timeout for genuinely unreachable addresses.
- Result: Achieved locally. The failing regression first proved that `/api/admin/omni-config/test` still used 30-second Responses color probes and a 45-second structured probe. The fix raises both Responses visual and structured preflight read timeouts to 120 seconds without changing `/models` reachability probing, request schemas, key handling, or parser semantics. A related stale test was aligned to the current runtime timeout-refresh contract, and existing mock fixtures were updated to include the current `timeout` field. Verification passed: focused RED/GREEN regression, admin model-config suite `56 passed`, Responses provider/image suite `61 passed`, the previously failing probe/circuit/smoke tests, `git diff --check`, and `./scripts/local-ci.sh --tests` with all 6 checks passing.
- Next step: Commit the exact local fix, open a production deployment CO for `miloco.esxi`, deploy the exact source SHA, then validate Miloco/OpenClaw health and the admin Responses probe behavior without exposing credentials, camera imagery, or raw model output.
