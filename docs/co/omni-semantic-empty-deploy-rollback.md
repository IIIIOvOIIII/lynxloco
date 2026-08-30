# Rollback for Miloco Omni semantic-empty prompt repair deployment

## Rollback triggers

Rollback or stop execution if any of the following occurs:

1. Uploaded artifact SHA256 values do not match the approved local evidence.
2. The installer/sync flow fails before a healthy Miloco service is available.
3. Miloco backend health does not recover within the maintenance window after one controlled restart.
4. The dashboard root becomes unreachable after deployment.
5. The runtime summary endpoint breaks core API startup or returns server errors unrelated to normal missing runtime data.
6. The OpenClaw gateway or `miloco-openclaw-plugin` does not recover after one controlled restart/refresh.
7. Post-deploy Omni diagnostics show a worse state than before deployment, such as request rejection or parser failure replacing the prior parser-clean `semantic_empty` state.

## Rollback plan

1. If failure occurs before any installer mutation, stop immediately and leave the current production service untouched.
2. If failure occurs after installer mutation, reinstall the prior deployed Miloco release from exact Git commit `ed6fdff96eab101684d7e27eb87c6548ad3f7170` by building that commit in an isolated local worktree and running the same bounded `dist/` + `scripts/` transfer plus remote install operations against `miloco.esxi`.
3. Restart only the existing Miloco/OpenClaw managed services needed to restore the prior package set.
4. Do not restore or overwrite the persistent Miloco configuration, Xiaomi account state, RTSP camera definitions, model API keys, or database unless separate evidence proves the deployment changed them.
5. Re-run the same post-rollback checks:
   - `miloco-cli service status` reports running and managed;
   - local `http://127.0.0.1:1810/health` returns OK;
   - LAN `http://miloco.esxi:1810/health` returns OK;
   - dashboard root returns HTTP 200 HTML;
   - OpenClaw gateway status is healthy;
   - `miloco-openclaw-plugin` is loaded.
6. Close the CO accurately:
   - `Successfully Closed` only if the new release remains deployed and all mandatory checks pass;
   - `Failed` if mutation occurred and rollback did not fully restore service;
   - `Not Executed` if the approval gate or pre-mutation checks prevented execution.

Do not print or store secrets, RTSP URLs, service tokens, API keys, camera images, raw prompts, raw model responses, or cookies during rollback.
