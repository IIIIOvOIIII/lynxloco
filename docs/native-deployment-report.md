# Native OpenClaw deployment profile

The existing `deploy.sh` now accepts `MILOCO_DEPLOY_RUNTIME=openclaw` for the registered production host. The default remains `docker`; its controller, artifact allowlist, build receipt and rollback behavior are unchanged.

## Operator interface

Configure the approved host mapping and `MILOCO_SSH_IDENTITY` through the existing governed deployment environment. The native profile accepts only `MILOCO_DEPLOY_PRODUCTION_HOST`, not either staging profile. Obtain the required CO/PAM approval before remote operations.

```bash
export MILOCO_DEPLOY_RUNTIME=openclaw
./deploy.sh build
./deploy.sh preflight --host "$MILOCO_DEPLOY_PRODUCTION_HOST"
./deploy.sh deploy --host "$MILOCO_DEPLOY_PRODUCTION_HOST"
./deploy.sh verify --host "$MILOCO_DEPLOY_PRODUCTION_HOST"
./deploy.sh status --host "$MILOCO_DEPLOY_PRODUCTION_HOST"
./deploy.sh rollback --host "$MILOCO_DEPLOY_PRODUCTION_HOST" "$TRANSACTION_SHA"
```

For native rollback, `TRANSACTION_SHA` is the full 40-character SHA of the **current deployment being undone**. It does not identify the restored software version. The controller restores that transaction's recorded predecessor. Docker rollback continues to take the version SHA to restore.

Build requires a clean tracked tree. The existing Linux x86_64 release archive and `.receipt` remain in `dist/lab/<sha>/`. Native build additionally writes immutable `miloco-lab-<sha>.openclaw.receipt`, binding the full Git SHA, archive SHA-256, original allowlist SHA-256, native controller SHA-256 and `openclaw-root-v1` profile. Deployment validates both receipts before contacting the host. Build with the native selector from the start; an existing immutable Docker build is not silently repackaged or assigned a new native receipt.

## Fixed runtime scope

The profile is restricted to Linux x86_64, root service ownership, `/root/.openclaw/miloco`, `/root/.local/share/uv/tools/{miloco,miloco-cli}`, backend Python `/root/.local/share/uv/tools/miloco/bin/python`, supervisor program `miloco-backend` and health endpoint `http://127.0.0.1:1810/health`. Managed native state is `/opt/miloco-openclaw`, accessible only to root.

Preflight performs no controller installation or release-directory creation. It checks platform, registered directory ownership and permissions, installed native tools and uv tool directory, configuration and supervisor command/home, the running process owner/command/home/executable, observability schema compatibility and disk space. It reports package versions and interpreter paths, without configuration values, tokens, package-manager output or application logs.

The transaction transfers one bounded allowlisted archive through SSH stdin, verifies digest and per-file checksum coverage, rejects links/special files/traversal and validates wheel metadata versions against the exact release SHA. Maximum compressed transfer is 2 GiB; expanded content is limited to 4 GiB and 20,000 entries. The existing archive includes models and acceptance assets, which are retained as release evidence; this profile does not replace the running models or OpenClaw plugin.

Before tool replacement, the controller copies both tool directories, configuration and supervisor configuration into an immutable transaction backup. Tool trees and configuration have SHA-256 verification records. All SQLite files below the registered home receive SQLite online backups into the root-only backup tree; the receipt records prior package versions, source short commit, Python path, base Python interpreter, configuration digest and observability schema version.

The old CLI stops the service before either tool changes. Both installs use the existing `uv tool install ... --force --reinstall` primitive, the release's locked export constraints, the previous resolved base Python interpreter and `--no-python-downloads`. Installation keeps `server.python_bin` and the existing configuration bytes unchanged, then invokes `miloco-cli service restart`. Acceptance verifies backend/CLI versions, unchanged base interpreter and configuration, the actual supervisor process and HTTP 200 health. Model concurrency configuration is a separate approved main-task step; deployment does not set it.

## Recovery behavior

By default, a failed install/restart/verification triggers restoration of both backed-up tools and configuration, followed by prior-version and health verification. A failure of restoration itself stops with an operator-action error and retains the backup. Completed deployment records are stored in `state.json`; release archives, receipts and prior tools remain available for inspection. No automatic garbage collection runs.

For an explicit user request to retain the result, set `MILOCO_OPENCLAW_FAILURE_POLICY=retain` on the native `deploy.sh deploy` invocation. The controller then records `deployment-failed-retained` and returns an error without restoring old tools/configuration on install/restart/verification failure. This cycle must not invoke rollback during acceptance. The prior default remains available for deployments without that opt-in.

Explicit native rollback is accepted only for the current successful or failed-retained transaction, after later operator authorization. It snapshots current SQLite state again, stops the service, restores tools/configuration/entrypoint links and verifies the recorded prior package versions, configuration and health. Replaced tools are retained under the transaction backup.

SQLite backups are **not copied over the live databases during application rollback**. All newly collected observability rows and additive columns remain in place. When the prior recorded schema is v4 and the live marker is v5, the controller first verifies the expected concurrency column names/types in `traces` and `traces_device`, then sets only `PRAGMA user_version=4`. It does not drop columns, replace tables or delete observations. Unsupported versions or an unrecognized v5 shape fail closed. The additive view is retained; a later v5 deployment can repeat its existing idempotent migration.

## Local verification

- `python3 -B -m unittest discover -s deploy/openclaw/tests -v`: passed. Coverage exercises invalid runtime/host routing, receipt profile/controller tampering, read-only streamed preflight and failure propagation, failed-preflight zero mutation, archive path/link rejection, complete simulated native deployment and rollback with new database records retained, failed-install automatic recovery, known v5 marker compatibility and rejection of unknown schema shapes.
- `bash -n deploy.sh deploy/openclaw/local.sh`: passed.
- Project Python `-B -m ruff check --no-cache deploy/openclaw/remote-release.py deploy/openclaw/tests/test_native_deploy.py`: passed.
- Existing `deploy/ai-lab/tests/test_deploy_contract.py`: 197 passed under the system Python; one acceptance collection test lacked `av` in that interpreter. That exact test passed when rerun using the existing project Python environment containing `av`. No Docker deployment implementation or tests were changed.
- `git diff --check`: passed for the assigned tracked change.

No production command, credential access, real uv installation, build, commit or push was performed by this implementation subtask. Local command/service fixtures prove orchestration and rollback scope; governed live preflight, artifact build, deployment and camera/provider acceptance remain the main task's responsibility. HTTP health is process/service evidence and does not prove camera or provider throughput.

## Coordination note

An initial lint invocation generated two ignored `.ruff_cache` entries outside the original narrow file list. This was reported immediately; the main agent explicitly authorized retaining these normal regenerable worktree outputs and continuing the bounded deliverable. They were not removed or reverted. Subsequent lint uses `--no-cache`.
