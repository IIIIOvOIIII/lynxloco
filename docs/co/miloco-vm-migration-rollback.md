Rollback triggers:
1. `docker.esxi` decommission cannot be verified safely.
2. `miloco.esxi` install fails before a healthy Miloco/OpenClaw-backed service is available.
3. Uploaded artifact SHA256 values do not match the approved values.
4. OpenClaw gateway or Miloco plugin installation creates an unstable host state that cannot be corrected within the maintenance window.

Rollback plan:
1. Stop any partially installed Miloco service on `miloco.esxi` with `miloco-cli service stop` if available; otherwise stop the recorded process or systemd user service identified during execution.
2. Stop and disable the OpenClaw gateway service on `miloco.esxi` only if it was installed by this change and is not an existing shared dependency. Move Miloco/OpenClaw state introduced by this change into a timestamped rollback archive under `/root/rollback-miloco-<CO>-<timestamp>`.
3. Remove the temporary loopback HTTP server and temporary install staging directory from `miloco.esxi` after preserving installer logs needed for diagnosis. Do not print secrets from logs.
4. If the old Docker service on `docker.esxi` must be restored, move the archived `/root/lynx-demise-<CO>-<timestamp>/opt-miloco` content back to `/opt/miloco`, then run the previous compose file with project name `miloco` and verify `http://docker.esxi:1811/health`.
5. If restoration is not required, keep the old Docker archive in place and leave `docker.esxi:1811` offline as the intended retired state.
6. Verify final state after rollback: either the old Docker endpoint is healthy again, or all partial new VM services are stopped and the old Docker endpoint remains intentionally retired.
