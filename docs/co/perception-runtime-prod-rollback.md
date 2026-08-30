# Rollback for Miloco perception runtime deployment

## Rollback triggers

Rollback or stop execution if any of the following occurs:

1. Uploaded artifact SHA256 values do not match the approved local evidence.
2. The official installer fails before a healthy Miloco service is available.
3. Miloco backend health does not recover within the maintenance window after one controlled restart.
4. The dashboard root becomes unreachable after deployment.
5. The authenticated runtime summary endpoint breaks core API startup or returns server errors unrelated to normal missing runtime data.
6. The OpenClaw gateway or `miloco-openclaw-plugin` does not recover after one controlled restart/refresh.

## Rollback plan

1. If failure occurs before any installer mutation, stop immediately, remove only the exact temporary staging directory created for this change, and leave the current production service untouched.
2. If failure occurs after installer mutation, stop Miloco with `miloco-cli service stop` if available.
3. Restore the CO-specific backups created before mutation:
   - restore `/root/.openclaw/miloco/config.json` from `/root/miloco-backup-<CO>-0ed57be-<timestamp>/config.json.pre-deploy` if it was backed up;
   - restore `/root/.openclaw/openclaw.json` from `/root/miloco-backup-<CO>-0ed57be-<timestamp>/openclaw.json.pre-deploy` if it was backed up.
4. Reinstall or reactivate the previous Miloco build using the previously retained production backup/cache for commit `2f58e93255ea775e09b15235d0c954c16392532a` if package-level rollback is required. If the previous installer cache or backup cannot be located during pre-checks, stop before mutation rather than proceeding without a package rollback path.
5. Restart Miloco through `miloco-cli service restart`.
6. Restart or refresh the OpenClaw gateway only if plugin recovery requires it.
7. Verify rollback state:
   - `miloco-cli service status` reports running and managed;
   - local `http://127.0.0.1:1810/health` returns OK;
   - LAN `http://miloco.esxi:1810/health` returns OK;
   - dashboard root returns HTTP 200 HTML;
   - OpenClaw gateway status is healthy;
   - `miloco-openclaw-plugin` is loaded.
8. Stop the temporary loopback HTTP server and remove only the exact temporary staging directory created for this change. Preserve the CO-specific backup directory for later diagnosis.
9. Close the CO accurately:
   - `Successfully Closed` only if the new release remains deployed and all mandatory checks pass;
   - `Failed` if mutation occurred and rollback did not fully restore service;
   - `Not Executed` if the approval gate or pre-mutation checks prevented execution.

Do not print or store secrets, RTSP URLs, service tokens, API keys, camera images, raw prompts, raw model responses, or cookies during rollback.
