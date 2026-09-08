# Native OpenClaw deployment profile

The existing `deploy.sh` now accepts `MILOCO_DEPLOY_RUNTIME=openclaw` for the registered production host. The default remains `docker`; its controller, artifact allowlist, build receipt and rollback behavior are unchanged.

## Operator interface

Configure the approved host mapping and `MILOCO_SSH_IDENTITY` through the existing governed deployment environment. The native profile accepts only `MILOCO_DEPLOY_PRODUCTION_HOST`, not either staging profile. Obtain the required CO/PAM approval before remote operations.

```bash
export MILOCO_DEPLOY_RUNTIME=openclaw
# Required for the 2026-09-08 user-controlled recovery policy:
export MILOCO_OPENCLAW_FAILURE_POLICY=retain
./deploy.sh build
./deploy.sh preflight --host "$MILOCO_DEPLOY_PRODUCTION_HOST"
./deploy.sh deploy --host "$MILOCO_DEPLOY_PRODUCTION_HOST"
./deploy.sh verify --host "$MILOCO_DEPLOY_PRODUCTION_HOST"
./deploy.sh status --host "$MILOCO_DEPLOY_PRODUCTION_HOST"
./deploy.sh rollback --host "$MILOCO_DEPLOY_PRODUCTION_HOST" "$TRANSACTION_SHA"
```

For native rollback, `TRANSACTION_SHA` is the full 40-character SHA of the **current deployment being undone**. It does not identify the restored software version. The controller restores that transaction's recorded predecessor. Docker rollback continues to take the version SHA to restore.

Build requires a clean tracked tree. The existing Linux x86_64 Docker release archive and `.receipt` remain unchanged in `dist/lab/<sha>/`. Native build additionally selects `openclaw` in the existing source build and creates `miloco-native-<sha>.tar.gz`. The upstream OpenClaw builder stamps the release version, builds its runtime, removes `devDependencies` from the packed package, and restores the source package metadata.

The native payload adds only `native/openclaw.tgz` and `native/manifest.json` to the original release contents and regenerates checksum coverage. The manifest binds the full Git SHA, original archive digest, plugin archive/version/file digests, and model archive/file digests. Plugin semver must match the backend PEP release base and pre/post/dev identifiers; an old plugin build is rejected. Optional plugin local version metadata must also match the backend when present.

The immutable `miloco-lab-<sha>.openclaw.receipt` now has schema 2 and binds both the original archive and native payload SHA-256, original allowlist SHA-256, native controller SHA-256 and `openclaw-root-v1` profile. Deployment validates both receipts before contacting the host, then streams only the native payload. Docker's allowlist and archive are not modified. Build with the native selector from the start; an existing immutable Docker build is not silently repackaged or assigned a new native receipt.

## Fixed runtime scope

The profile is restricted to Linux x86_64, root service ownership, `/root/.openclaw/miloco`, `/root/.local/share/uv/tools/{miloco,miloco-cli}`, backend Python `/root/.local/share/uv/tools/miloco/bin/python`, supervisor program `miloco-backend` and health endpoint `http://127.0.0.1:1810/health`. Managed native state is `/opt/miloco-openclaw`, accessible only to root.

Preflight performs no controller installation or release-directory creation. It checks platform, registered directory ownership and permissions, installed native tools and uv tool directory, configuration and supervisor command/home, the running process owner/command/home/executable, observability schema compatibility and disk space. The asset profile additionally requires the real models directory at `/root/.openclaw/miloco/models`, plugin at `/root/.openclaw/extensions/miloco-openclaw-plugin`, JSON configuration at `/root/.openclaw/openclaw.json`, supported OpenClaw commands, gateway RPC health and the loaded plugin version/source. Unsupported model or OpenClaw path overrides fail clearly. It reports package versions, interpreter paths and model digests without configuration contents, tokens, package-manager output or application logs.

The transaction transfers one bounded native payload through SSH stdin, verifies digest and per-file checksum coverage, rejects links/special files/traversal and validates wheel metadata versions against the exact release SHA. Maximum compressed transfer is 2 GiB; expanded content is limited to 4 GiB and 20,000 entries. Nested model/plugin archives receive separate member validation and file digests. Plugin runtime files must be present, and development dependencies or bundled `node_modules` are rejected.

The running model set follows the upstream flat layout: `det_4C.onnx`, `silero_vad.onnx`, `bge-small-zh-v1.5-int8.onnx`, `human_body_reid_v2.onnx`, and `bge-small-zh-v1.5-tokenizer.json`. Installation overwrites the shipped names, preserving unrelated files as the upstream installer does, and verifies every installed model hash. The OpenClaw plugin is installed through `openclaw plugins install --force` from the bound local archive. As in `scripts/install.py`, the controller probes install help and adds `--accept-capabilities` only where supported; partial timeout output containing that flag is recognized, while an inconclusive timed-out probe fails explicitly.

Before any service or asset mutation, the controller copies both tool directories, Miloco configuration, supervisor configuration, the entire models directory, installed plugin directory and OpenClaw configuration into an immutable transaction backup. Tool/asset trees and configuration have SHA-256 verification records. All SQLite files below the registered home receive SQLite online backups into the root-only backup tree; the receipt records prior package versions, source short commit, Python path, base Python interpreter, configuration digest and observability schema version. OpenClaw's own installer may update its plugin installation metadata or capability grants; the prior full OpenClaw JSON is retained for explicit recovery.

The controller stops the gateway, then uses the old CLI to stop the backend before either tool changes. Both tool installs use the existing `uv tool install ... --force --reinstall` primitive, the release's locked export constraints, the previous resolved base Python interpreter and `--no-python-downloads`. After models and plugin are installed and verified, it refreshes the plugin registry, restarts the gateway, verifies gateway RPC and the actually loaded plugin version/source, and restarts/verifies the backend. Installation keeps `server.python_bin` and all Miloco configuration bytes unchanged, including the current concurrency 8 and timeout/window 180 values. This release does not set or reset those values.

## Recovery behavior

The existing default remains automatic rollback for backward compatibility. The approved 2026-09-08 cycle explicitly selects `MILOCO_OPENCLAW_FAILURE_POLICY=retain`: any install/restart/verification failure records `deployment-failed-retained`, the expected asset manifest and `rollback_performed=false`, then returns an error without restoring tools, models, plugin or either configuration. Current files and backups remain for the user's decision. Completed deployment records include the accepted asset manifest for subsequent verification. No automatic garbage collection runs.

For an explicit user request to retain the result, set `MILOCO_OPENCLAW_FAILURE_POLICY=retain` on the native `deploy.sh deploy` invocation. The controller then records `deployment-failed-retained` and returns an error without restoring old tools/configuration on install/restart/verification failure. This cycle must not invoke rollback during acceptance. The prior default remains available for deployments without that opt-in.

Explicit native rollback is accepted only for the current successful or failed-retained transaction, after later operator authorization. It snapshots current SQLite state again, stops gateway/backend, restores tools, models, plugin, Miloco/OpenClaw configuration and entrypoint links, then verifies prior versions, plugin load, configuration and health. Replaced tools/models/plugin are retained under the transaction backup. A failed-retained install can have missing current package files; recovery preflight permits that shape and uses checked backup data, with the old backed-up CLI interpreter available if the current CLI cannot stop the backend.

Backups made before native asset delivery have no `assets_before` record. The new controller rejects those legacy backups before service mutation with `legacy native backup requires its recorded historical controller`. Use the historical clean checkout and `deploy.sh rollback` corresponding to that backup's recorded controller digest for such a transaction; those older backups cannot promise model/plugin restoration. New transactions always contain the full asset backup record.

SQLite backups are **not copied over the live databases during application rollback**. All newly collected observability rows and additive columns remain in place. When the prior recorded schema is v4 and the live marker is v5, the controller first verifies the expected concurrency column names/types in `traces` and `traces_device`, then sets only `PRAGMA user_version=4`. It does not drop columns, replace tables or delete observations. Unsupported versions or an unrecognized v5 shape fail closed. The additive view is retained; a later v5 deployment can repeat its existing idempotent migration.

## Local verification

- 2026-09-08: `python3 -B -m unittest discover -s deploy/openclaw/tests -v`: 19 passed. RED/GREEN demonstrated rejection of omitted assets and a stale plugin build. Coverage includes development-dependency rejection, old/new/partial-timeout capability help, loaded gateway/plugin validation, full asset deployment/rollback, failed plugin install retention followed by explicit rollback, configuration 8/180 preservation, and preservation of new SQLite observations.
- `bash -n deploy.sh deploy/openclaw/local.sh`: passed.
- Project Python `-B -m ruff check --no-cache deploy/openclaw/remote-release.py deploy/openclaw/tests/test_native_deploy.py`: passed.
- Historical 2026-09-06 Docker contract verification: 197 passed under the system Python; one acceptance collection test lacked `av` in that interpreter and passed when rerun using the project Python. The 2026-09-08 change preserves the Docker package selection/allowlist/controller and adds OpenClaw to the source-build package list only when the native runtime is explicitly selected.
- `git diff --check`: passed for the assigned tracked change.

No production command, credential access, real uv installation, build, commit or push was performed by this implementation subtask. Local command/service fixtures prove orchestration and rollback scope; governed live preflight, artifact build, deployment and camera/provider acceptance remain the main task's responsibility. HTTP health is process/service evidence and does not prove camera or provider throughput.

## Historical coordination note (2026-09-06)

An initial lint invocation generated two ignored `.ruff_cache` entries outside the original narrow file list. This was reported immediately; the main agent explicitly authorized retaining these normal regenerable worktree outputs and continuing the bounded deliverable. They were not removed or reverted. Subsequent lint uses `--no-cache`.
