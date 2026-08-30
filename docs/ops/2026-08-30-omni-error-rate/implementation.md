Pre-checks

1. Confirm the Change Order is in Implement state and PAM is active for root on miloco.esxi before any SSH access.
2. Confirm the Miloco backend and OpenClaw gateway are the intended live services on miloco.esxi.
3. Confirm the current Miloco source/version and working configuration without printing API keys, RTSP URLs, Xiaomi tokens, service tokens, camera images, or raw model responses.

Execution steps

1. Inspect the current Omni health path through Miloco management APIs and capture only safe fields: health state, error code, latency, configured protocol/model/base URL host, and whether an API key is present.
2. Query /api/stats summary and recent Omni error series to confirm whether the visible 100 percent error rate is current or historical-window-derived.
3. Run a bounded provider probe from miloco.esxi using the configured OpenAI Responses endpoint and existing runtime key, recording only HTTP status, response envelope type, visible output-text presence, usage presence, and safe Miloco error code.
4. Inspect recent Miloco logs with redaction for bearer tokens, API keys, RTSP URLs, Xiaomi tokens, dashboard service tokens, request bodies, raw provider responses, and camera imagery.
5. If the evidence proves a configuration or service-state fault, apply the smallest reversible repair: adjust the active Omni profile, trigger the existing retry endpoint, or restart only the affected Miloco/OpenClaw service.
6. If the evidence proves a Miloco code defect, stop before production code deployment and prepare a test-first source fix with a separate exact-SHA deployment Change Order.

Verification steps

1. Re-run the Miloco Omni configuration test and confirm ok=true with a safe code/message.
2. Confirm the Omni health banner clears or reports an OK/closed circuit state.
3. Confirm /api/stats no longer shows new Omni failures after at least one fresh perception cycle, or explicitly document if the remaining 100 percent value is due only to the selected historical window.
4. Confirm Miloco /health remains OK and the OpenClaw plugin/gateway remain reachable.
5. Close the Change Order with exact non-secret evidence and final status.
