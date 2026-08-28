# Miloco ai-lab Simplified Immutable Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace destructive automatic history retention with immutable reuse-or-fail releases, package exact RTSP/Responses fixture acceptance, and deploy one clean SHA sequentially to `ai-lab01.esxi` and `ai-lab02.esxi` with rollback and measured/not-measured evidence.

**Architecture:** The workstation builds a receipt-bound release from one exact clean Git SHA. Under one remote host lock, the controller classifies that SHA as `new`, `existing`, or `probe_error`: only `new` may publish and build, while `existing` may only prove `capable` and reuse without mutation. Published releases, canonical image pairs, receipts, and acceptance markers accumulate immutably; the 5 GiB preflight stops new work instead of deleting history.

**Tech Stack:** Bash, Git, `uv`, pnpm, Python 3.12, Docker Engine 26+, Docker Compose 2.26+, FastAPI/Uvicorn, PyAV, pytest, SHA-256 manifests, SSH.

**Spec:** `docs/superpowers/specs/2026-08-28-ai-lab-simplified-deployment-design.md`

**Supersedes:** The retention, same-SHA rebuild, and Tasks 4–7 execution text in
`docs/superpowers/plans/2026-08-28-ai-lab-deployment.md`. That earlier plan is
historical evidence and must not be used to authorize deletion or host work.

## Global Constraints

- Target only `ai-lab01.esxi` and `ai-lab02.esxi`; these lab hosts are exempt from CO and PAM.
- Deploy only `linux/amd64` and the official pinned base image `python:3.12-slim-bookworm@sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579`.
- Use one exact clean 40-character Git SHA for both hosts. Never rebuild between lab01 and lab02.
- Public operations remain exactly `build`, `preflight`, `deploy`, `verify`, `status`, and `rollback`.
- Automatic deletion of a published release directory, canonical image pair, artifact receipt, or acceptance marker is forbidden.
- An existing SHA is immutable: `capable` reuses it with zero build/tag/marker mutation; invalid or uncertain proof fails with zero mutation.
- Only a SHA with no release directory, artifact receipt, acceptance marker, or canonical runtime/acceptance tag may enter the new-release build path.
- The existing 5 GiB free-disk preflight remains a hard non-destructive stop.
- Candidate-only cleanup may remove unpublished temporary archives, extraction staging, isolated candidate tags, failed candidate containers, and unpublished atomic-write files.
- Activation failure must restore the former capable release and must preserve every published proof for both SHAs.
- Runtime remains UID/GID `10001:10001`, host networking on port `1810`, read-only root filesystem, state at `/opt/miloco-lab/state`, and deployment records at `/opt/miloco-lab/deploy-state`.
- `ai-lab01.esxi` uses `3.0` CPUs and `3072m`; `ai-lab02.esxi` uses `1.25` CPUs and `1536m`.
- Actual SSH operations explicitly set `MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw`; the controller must pass `-i` and `IdentitiesOnly=yes` without printing key material.
- Never include credentials, credential-bearing RTSP URIs, API keys, authorization headers, request/response bodies, base64 images, local configuration, `.git`, caches, or virtual environments in artifacts, logs, documents, or commits.
- Fixture acceptance does not prove a real camera or real VLM. Without actual endpoint variables, record both as `not_measured`.
- Do not run the mutating `task lint`. Preserve and report the existing repository-wide format/type and macOS node-monitor baselines separately.
- Do not push to `XiaoMi/xiaomi-miloco`.

---

### Task 1: Make published releases immutable and remove destructive retention

**Files:**
- Modify: `deploy.sh`
- Modify: `deploy/ai-lab/remote-release.sh`
- Modify: `deploy/ai-lab/tests/test_deploy_contract.py`
- Modify: `deploy/ai-lab/README.md`

**Interfaces:**
- Consumes: the current receipt-bound archive stream, remote controller self-hash, `release_capability SHA -> capable|definitively_invalid|probe_error`, canonical runtime tag `miloco-lab:<sha>`, and canonical acceptance tag `miloco-lab-acceptance:<sha>`.
- Produces: `published_sha_state SHA -> new|existing|probe_error`; mode-aware receive/build functions that enforce new-only publishing and existing-only reuse; activation without retention; explicit SSH identity handling.

- [ ] **Step 1: Write RED tests for the immutable-state decision**

  Add executable tests to `test_deploy_contract.py` that establish four states before any archive publication, marker change, image build, or canonical tag action:

  ```python
  @pytest.mark.parametrize(
      ("filesystem_evidence", "runtime_tag", "acceptance_tag", "expected"),
      [
          ("none", "absent", "absent", "new"),
          ("release", "absent", "absent", "existing"),
          ("artifact", "absent", "absent", "existing"),
          ("accepted", "absent", "absent", "existing"),
          ("none", "present", "absent", "existing"),
          ("none", "absent", "present", "existing"),
          ("none", "probe_error", "absent", "probe_error"),
      ],
  )
  def test_published_sha_state_is_new_only_when_every_durable_object_is_absent(
      tmp_path: Path,
      filesystem_evidence: str,
      runtime_tag: str,
      acceptance_tag: str,
      expected: str,
  ) -> None:
      result, mutation_log = _run_published_sha_state_harness(
          tmp_path,
          filesystem_evidence=filesystem_evidence,
          runtime_tag=runtime_tag,
          acceptance_tag=acceptance_tag,
      )
      assert result.returncode == 0
      assert result.stdout.strip() == expected
      assert mutation_log == []
  ```

  Add `_run_published_sha_state_harness` beside the existing shell-function
  harnesses. It must extract the real `published_sha_state` body, create only
  the requested release/record evidence under the pytest temporary directory,
  stub the strict image-reference query with the two parameter values, and
  append every filesystem or Docker mutation attempt to the returned list.

  The test harness must prove that a successful Docker query confirming both canonical tags absent is required before returning `new`. A Docker query error or malformed successful output returns `probe_error`, not `new`.

- [ ] **Step 2: Write RED tests for reuse-or-fail behavior**

  Add tests that invoke the real decision branch with stubs only below the Docker/filesystem boundary:

  ```python
  @pytest.mark.parametrize("capability", ["definitively_invalid", "probe_error"])
  def test_existing_sha_never_rebuilds_or_mutates_when_not_capable(
      tmp_path: Path, capability: str
  ) -> None:
      result, mutation_log = _run_existing_sha_transaction_harness(
          tmp_path, capability=capability, state_pointer="none"
      )
      assert result.returncode != 0
      assert mutation_log == []

  @pytest.mark.parametrize("state_pointer", ["current", "previous", "history", "none"])
  def test_existing_capable_sha_reuses_without_build_tag_or_marker_mutation(
      tmp_path: Path, state_pointer: str
  ) -> None:
      result, mutation_log = _run_existing_sha_transaction_harness(
          tmp_path, capability="capable", state_pointer=state_pointer
      )
      assert result.returncode == 0
      assert mutation_log == []
  ```

  `_run_existing_sha_transaction_harness` must source the real decision branch,
  substitute lab-root constants with temporary paths, and stub publication,
  atomic writes, Docker build/tag/remove, Compose, and record deletion so that
  every attempted mutation is observable in `mutation_log`.

  `mutation_log` must fail on calls to release publication, artifact-record write, marker invalidation/write, runtime/acceptance build, candidate/canonical tag removal or promotion, and published-record deletion. Parameterize the capable case for `current`, `previous`, non-pointer history, and no state pointer.

- [ ] **Step 3: Write RED tests forbidding historical deletion**

  Replace the old retention tests with contracts requiring:

  ```python
  def test_activation_has_no_historical_retention_or_pair_removal():
      remote = REMOTE_RELEASE.read_text(encoding="utf-8")
      activation = function_body(remote, "activate_release")
      assert "retain_rollback_history" not in activation
      assert "remove_release_pair" not in activation
      assert "activated_cleanup_failed" not in activation
      assert "retain_rollback_history()" not in remote
      assert "remove_release_pair()" not in remote
  ```

  Add executable success and activation-failure harnesses. Snapshot release directories, canonical image IDs, artifact receipts, and acceptance markers before the operation and assert byte/ID equality afterward. A failed candidate container and isolated candidate tags may disappear; published proof may not.

- [ ] **Step 4: Run focused RED tests**

  Run:

  ```bash
  backend/.venv/bin/python -m pytest -q deploy/ai-lab/tests/test_deploy_contract.py \
    -k 'published_sha_state or existing_sha_never or existing_capable or historical_retention or published_proof'
  ```

  Expected: failures because the current controller treats confirmed-invalid unprotected SHAs as rebuild targets and calls `retain_rollback_history` after activation.

- [ ] **Step 5: Implement `published_sha_state` before archive publication**

  In `remote-release.sh`, add a read-only function with this contract:

  ```bash
  published_sha_state() {
      local sha="$1" runtime_state acceptance_state
      validate_sha "$sha"

      if [[ -e "$RELEASES_DIR/$sha" || -L "$RELEASES_DIR/$sha" \
          || -e "$ARTIFACT_RECORDS_DIR/$sha" || -L "$ARTIFACT_RECORDS_DIR/$sha" \
          || -e "$ACCEPTED_DIR/$sha" || -L "$ACCEPTED_DIR/$sha" ]]; then
          printf 'existing\n'
          return 0
      fi

      runtime_state="$(image_reference_state "miloco-lab:$sha")"
      acceptance_state="$(image_reference_state "miloco-lab-acceptance:$sha")"
      if [[ "$runtime_state" == "absent" && "$acceptance_state" == "absent" ]]; then
          printf 'new\n'
      elif [[ "$runtime_state" == "probe_error" || "$acceptance_state" == "probe_error" ]]; then
          printf 'probe_error\n'
      elif [[ "$runtime_state" == present:sha256:* || "$acceptance_state" == present:sha256:* ]]; then
          printf 'existing\n'
      else
          printf 'probe_error\n'
      fi
  }
  ```

  Reuse the controller's strict image-output parser; do not create a second looser parser. Run this inside the existing host lock before `receive_release_locked` can publish a release or record.

- [ ] **Step 6: Make receive and build mode-aware**

  Change the internal interfaces to carry the pre-mutation state explicitly:

  ```bash
  receive_release_locked "$sha_state" "$sha" "$archive_digest" \
      "$controller_digest" "$allowlist_digest"
  build_images_and_accept "$sha_state" "$host" "$sha" "$release"
  ```

  Required behavior:

  ```bash
  case "$sha_state" in
      new)
          # require release/records/marker/canonical tags still absent;
          # publish verified release + artifact record; build isolated candidates.
          ;;
      existing)
          # receive only into private temporary input for digest/receipt comparison;
          # require existing release + artifact record exact; never publish.
          [[ "$(release_capability "$sha")" == "capable" ]] \
              || die 4 "existing release is not reusable"
          return 0
          ;;
      probe_error|*)
          die 4 "release publication state is uncertain"
          ;;
  esac
  ```

  Recheck `published_sha_state` immediately before new-mode publication to close a time-of-check/time-of-use drift inside the transaction. Because the host lock is held, any state change means unexpected external mutation and must fail closed.

- [ ] **Step 7: Remove historical retention and pair deletion**

  Remove `retain_rollback_history`, `remove_release_pair`, the post-commit `activated_cleanup_failed` branch, and tests/documentation that promise two retained histories. Successful activation ends after `current` is atomically committed and the transition trap is disarmed:

  ```bash
  atomic_write "$CURRENT_FILE" "$sha"
  commit_transition
  return 0
  ```

  Keep `remove_candidate_image_tags`, candidate-container removal, incoming/staging cleanup, and unpublished atomic-write cleanup. Do not broaden these helpers to canonical published tags.

- [ ] **Step 8: Preserve accepted proof across activation failure**

  Update compensation tests and code so that activation failure restores the former capable service but leaves the new accepted SHA's release directory, canonical runtime/acceptance tags, artifact receipt, and acceptance marker intact. Only the failed candidate container may be removed.

  The transition trap must still return exit `70` when service restoration itself fails.

- [ ] **Step 9: Add explicit SSH identity handling**

  In root `deploy.sh`, read `MILOCO_SSH_IDENTITY` only for remote operations. Require an absolute, regular, non-symlink file owned by the current effective user with no group/other permission bits, then construct the SSH prefix as an array:

  ```bash
  ssh_args=(-o BatchMode=yes -o IdentitiesOnly=yes -i "$MILOCO_SSH_IDENTITY")
  ssh "${ssh_args[@]}" -- "$target_host" bash -s -- "$@"
  ```

  Tests must set a temporary mode-0600 identity and assert `-i`, `IdentitiesOnly=yes`, and the exact path are passed without reading or printing file content. Missing, relative, symlinked, wrong-owner where testable, or permission-unsafe identity must fail before SSH. `build` and `--help` do not require the variable.

- [ ] **Step 10: Update the operator contract**

  Rewrite `deploy/ai-lab/README.md` to remove:

  ```text
  retention
  two historical pairs
  invalid debris deletion
  remove_release_pair
  activated_cleanup_failed
  same-SHA rebuild
  ```

  Document immutable accumulation, reuse-or-fail existing SHAs, 5 GiB hard stop, permitted transient cleanup, explicit SSH identity, and the future separately governed cleanup boundary.

- [ ] **Step 11: Run Task 1 verification and commit**

  Run:

  ```bash
  backend/.venv/bin/python -m pytest -q deploy/ai-lab/tests/test_deploy_contract.py
  backend/.venv/bin/ruff check deploy/ai-lab/tests/test_deploy_contract.py
  bash -n deploy.sh deploy/ai-lab/remote-release.sh deploy/ai-lab/container-entrypoint.sh
  ./deploy.sh --help
  git diff --check
  ```

  Do not run Docker, SSH, remote status, or `task lint` in this task.

  Commit:

  ```bash
  git add deploy.sh deploy/ai-lab/remote-release.sh \
    deploy/ai-lab/tests/test_deploy_contract.py deploy/ai-lab/README.md
  git commit -m "fix(deploy): make lab releases immutable"
  ```

---

### Task 2: Package exact RTSP and Responses fixture acceptance

**Files:**
- Modify: `deploy.sh`
- Modify: `deploy/ai-lab/Dockerfile`
- Modify: `deploy/ai-lab/tests/test_deploy_contract.py`
- Modify: `scripts/rtsp-view-smoke.sh`
- Modify: `scripts/responses-vlm-smoke.sh`
- Modify only when installed-wheel portability requires it: `backend/miloco/tests/integration/test_rtsp_live_view.py`
- Modify only when installed-wheel portability requires it: `backend/miloco/tests/integration/test_responses_perception.py`
- Create during artifact build only: `acceptance/integration/`, `acceptance/fixtures/rtsp/`, `acceptance/scripts/`

**Interfaces:**
- Consumes: committed production integration tests, `responses_fixture_server.py`, RTSP fixtures, installed Miloco wheels, and Task 1's new-only build transaction.
- Produces: one bounded acceptance tree and acceptance-image command proving RTSP perception, shared-session Uvicorn WebSocket live view, Responses JSON/SSE/no-key/Bearer/parser/breaker behavior before activation.

- [ ] **Step 1: Write RED tests for the exact acceptance tree**

  Require the staging tree to contain only:

  ```text
  acceptance/pytest.ini
  acceptance/integration/test_rtsp_perception.py
  acceptance/integration/test_rtsp_live_view.py
  acceptance/integration/test_responses_perception.py
  acceptance/integration/responses_fixture_server.py
  acceptance/fixtures/rtsp/**
  acceptance/scripts/rtsp-view-smoke.sh
  acceptance/scripts/responses-vlm-smoke.sh
  ```

  Assert no `conftest.py`, unrelated tests, user config, `.env`, credentials,
  `.git`, cache, venv, or source package tree enters `acceptance/`. Assert both
  smoke scripts retain executable mode after staging.

- [ ] **Step 2: Write RED tests for portable smoke Python selection**

  `rtsp-view-smoke.sh` must implement this exact selection rule while retaining
  its current `uv` fallback:

  ```bash
  if [[ -n "${MILOCO_SMOKE_PYTHON:-}" ]]; then
      [[ "$MILOCO_SMOKE_PYTHON" == /* && -f "$MILOCO_SMOKE_PYTHON" \
          && ! -L "$MILOCO_SMOKE_PYTHON" && -x "$MILOCO_SMOKE_PYTHON" ]] \
          || exit 2
      exec "$MILOCO_SMOKE_PYTHON" - "$CAMERA_ID" "$BACKEND_URL" "$DURATION_SEC"
  else
      command -v uv >/dev/null 2>&1 || exit 2
      cd "$REPO_ROOT/backend"
      exec uv run python - "$CAMERA_ID" "$BACKEND_URL" "$DURATION_SEC"
  fi
  ```

  `responses-vlm-smoke.sh` must implement the corresponding exact executable
  selection while retaining its repository-venv fallback:

  ```bash
  if [ -n "${MILOCO_SMOKE_PYTHON:-}" ]; then
      [ "${MILOCO_SMOKE_PYTHON#/}" != "$MILOCO_SMOKE_PYTHON" ] \
          && [ -f "$MILOCO_SMOKE_PYTHON" ] \
          && [ ! -L "$MILOCO_SMOKE_PYTHON" ] \
          && [ -x "$MILOCO_SMOKE_PYTHON" ] || exit 2
      python_bin="$MILOCO_SMOKE_PYTHON"
  else
      python_bin="$repo_root/backend/.venv/bin/python"
      [ -f "$python_bin" ] && [ ! -L "$python_bin" ] && [ -x "$python_bin" ] || exit 2
  fi
  exec "$python_bin" - 2>/dev/null
  ```

  Both override paths use direct `exec`, never `eval` or word splitting. The
  override value is never echoed.

  Tests must reject relative, symlinked, non-file, non-executable, and
  multi-word values; the path must never be echoed or evaluated by a shell.

- [ ] **Step 3: Run focused RED tests**

  Run:

  ```bash
  backend/.venv/bin/python -m pytest -q deploy/ai-lab/tests/test_deploy_contract.py \
    -k 'acceptance_tree or smoke_python or acceptance_command'
  ```

  Expected: failures because the acceptance stage still runs only
  `-m ai_lab_fixture` and the smoke scripts do not yet honor the override.

- [ ] **Step 4: Stage the exact installed-wheel fixtures**

  Update the build staging function in `deploy.sh` to copy each selected file
  explicitly, not a whole test directory. Preserve fixture subdirectories and
  normalize test files to `0644`, fixture assets to `0644`, directories to
  `0755`, and smoke scripts to `0555` in the release.

  Re-run the artifact allowlist validation after staging. Any unexpected
  acceptance path fails the build.

- [ ] **Step 5: Configure the acceptance image**

  In the acceptance stage set:

  ```dockerfile
  ENV PYTHONPATH=/acceptance/integration \
      MILOCO_CONFIG_SEARCH_PATH=/tmp/miloco-acceptance-config \
      MILOCO_SMOKE_PYTHON=/usr/local/bin/python \
      MILOCO_MODEL__OMNI__API_KEY= \
      MILOCO_RESPONSES_API_KEY=
  ```

  Copy the bounded tree to `/acceptance/`, keep `ENTRYPOINT []`, and use this
  exact command:

  ```dockerfile
  CMD ["python", "-m", "pytest", "-q", "/acceptance/integration/test_rtsp_perception.py", "/acceptance/integration/test_rtsp_live_view.py::test_fixture_perception_and_uvicorn_live_view_share_one_rtsp_session", "/acceptance/integration/test_responses_perception.py", "-k", "not smoke"]
  ```

  Preserve production `miloco.*` imports, real PyAV fixture decoding, real
  localhost Uvicorn/WebSocket/httpx paths, current parser and breaker behavior,
  and no-key/Bearer fixture assertions. Only repository-layout subprocess smoke
  tests are deselected in the installed-wheel image.

- [ ] **Step 6: Run Task 2 verification and commit**

  Run:

  ```bash
  backend/.venv/bin/python -m pytest -q deploy/ai-lab/tests/test_deploy_contract.py
  backend/.venv/bin/python -m pytest -q \
    backend/miloco/tests/integration/test_rtsp_perception.py \
    backend/miloco/tests/integration/test_rtsp_live_view.py::test_fixture_perception_and_uvicorn_live_view_share_one_rtsp_session \
    backend/miloco/tests/integration/test_responses_perception.py \
    -k 'not smoke'
  backend/.venv/bin/ruff check deploy/ai-lab/tests/test_deploy_contract.py \
    backend/miloco/tests/integration/test_rtsp_live_view.py \
    backend/miloco/tests/integration/test_responses_perception.py
  bash -n deploy.sh deploy/ai-lab/remote-release.sh \
    scripts/rtsp-view-smoke.sh scripts/responses-vlm-smoke.sh
  git diff --check
  ```

  Real Docker acceptance remains deferred to the lab01 new-SHA transaction if
  no local Docker daemon is available.

  Commit only files actually required:

  ```bash
  git add deploy.sh deploy/ai-lab/Dockerfile \
    deploy/ai-lab/tests/test_deploy_contract.py \
    scripts/rtsp-view-smoke.sh scripts/responses-vlm-smoke.sh \
    backend/miloco/tests/integration/test_rtsp_live_view.py \
    backend/miloco/tests/integration/test_responses_perception.py
  git commit -m "test(deploy): package lab acceptance fixtures"
  ```

---

### Task 3: Build once, deploy, and validate `ai-lab01.esxi`

**Files:**
- Update only in the ignored SDD workspace during lab01: `.superpowers/sdd/2026-08-28-ai-lab-simplified-deployment/progress.md`
- Do not modify tracked files between the exact build and lab02 deployment.

**Interfaces:**
- Consumes: Task 2's clean HEAD, mode-0600 SSH identity `/Users/nicholasliao/.ssh/id_co_openclaw`, new immutable release transaction, lab01 resource profile.
- Produces: one built archive/receipt and a healthy lab01 deployment whose exact SHA remains the local clean HEAD for Task 4.

- [ ] **Step 1: Verify local release identity and SSH-key boundary**

  Run read-only checks:

  ```bash
  git status --porcelain
  git rev-parse HEAD
  stat -f '%Su %Lp %N' /Users/nicholasliao/.ssh/id_co_openclaw
  ```

  Require a clean worktree, current-user ownership, regular non-symlink key,
  and no group/other permission bits. Record only the key path and structural
  result; never read or print key contents.

- [ ] **Step 2: Build one exact release**

  Run:

  ```bash
  ./deploy.sh build
  ```

  Validate the mode-0444 receipt, archive SHA, full Git SHA, controller digest,
  allowlist digest, artifact path, tar allowlist, and absence of forbidden
  paths or credential patterns. Save the exact release SHA and archive digest
  in the ignored SDD ledger.

- [ ] **Step 3: Run read-only lab01 preflight**

  Run:

  ```bash
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw \
    ./deploy.sh preflight ai-lab01.esxi
  ```

  Confirm Debian Linux amd64, Docker `>=26`, Compose `>=2.26`, Python 3,
  required GNU utilities, at least 5 GiB free, port `1810` free or owned by the
  recorded Miloco container, and lab01's `3.0` CPU/`3072m` profile. Do not
  mutate unrelated containers.

- [ ] **Step 4: Deploy lab01 through the new-SHA transaction**

  Run:

  ```bash
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw \
    ./deploy.sh deploy ai-lab01.esxi
  ```

  Expected sequence: receipt check, immutable controller, one archive stream,
  one host lock, new-state proof, extraction verification, runtime and
  acceptance builds, exact fixture acceptance, marker publication, activation,
  strict health gate, and no historical retention scan or deletion.

  On failure, verify former service restoration when applicable, preserve all
  published proof, record fixed metadata only, and stop before lab02.

- [ ] **Step 5: Verify runtime, resources, and immutable proof**

  Run:

  ```bash
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw \
    ./deploy.sh verify ai-lab01.esxi
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw \
    ./deploy.sh status ai-lab01.esxi
  ```

  Confirm exact SHA, health, HTTP 200, UID/GID `10001:10001`, read-only root,
  owner-only state, host network port `1810`, `3.0` CPU/`3072m`, restart count,
  fixture acceptance result, and free disk after deployment. Inspect only
  structural environment names/values already fixed in Compose; do not read
  application logs or secret-bearing runtime configuration.

- [ ] **Step 6: Validate the browser surface**

  Using the in-app browser, open `http://ai-lab01.esxi:1810/` and verify:

  ```text
  dashboard loads
  RTSP camera type and fields render
  model protocol selector includes Responses
  keyless Responses profile is representable
  generic camera watch route renders
  no console-blocking error
  ```

  Do not create a persistent synthetic camera or profile merely to make the UI
  pass.

- [ ] **Step 7: Record real-endpoint boundaries without tracked edits**

  If `MILOCO_RTSP_TEST_URL` exists, run the RTSP smoke and view smoke using the
  existing safe temporary-camera cleanup. If a real Responses URL/model exists,
  run the Responses VLM smoke. Otherwise record each as `not_measured`.

  Write lab01 evidence only to this plan's ignored SDD ledger. Keep the tracked
  worktree clean so lab02 can deploy the exact same HEAD and receipt.

---

### Task 4: Deploy the identical release to `ai-lab02.esxi`

**Files:**
- Update only in the ignored SDD workspace during activation: `.superpowers/sdd/2026-08-28-ai-lab-simplified-deployment/progress.md`
- Modify after both hosts are green: `docs/2026-08-28-rtsp-responses-support_PROGRESS.md`

**Interfaces:**
- Consumes: the exact archive, receipt, Git SHA, and digest already proven on lab01; lab02 resource profile.
- Produces: an independently healthy lab02 deployment with the same runtime SHA, followed by one tracked dual-host evidence update.

- [ ] **Step 1: Prove no release drift**

  Confirm:

  ```bash
  release_sha="$(git rev-parse HEAD)"
  receipt="dist/lab/$release_sha/miloco-lab-$release_sha.receipt"
  test -z "$(git status --porcelain)"
  test -f "$receipt"
  test "$release_sha" = "$(awk -F= '$1==\"git_sha\" {print $2}' "$receipt")"
  ```

  Recompute archive/controller/allowlist digests from the exact SHA-addressed
  receipt and require equality with the lab01 values in the ignored ledger. Do
  not run `build` again.

- [ ] **Step 2: Preflight and deploy lab02**

  Run:

  ```bash
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw \
    ./deploy.sh preflight ai-lab02.esxi
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw \
    ./deploy.sh deploy ai-lab02.esxi
  ```

  Confirm lab02's `1.25` CPU/`1536m` profile, at least 5 GiB free, and the same
  archive digest and SHA as lab01. A lab02 failure restores only lab02 and must
  not alter lab01.

- [ ] **Step 3: Verify constrained runtime and observe idle stability**

  Run `verify` and `status` with the same explicit identity. Record exact SHA,
  health, HTTP 200, UID/GID, state permissions, restart count, CPU/memory
  limits, free disk, and fixture result. Observe the service for five minutes
  without a blocking sleep longer than 60 seconds; sample status in bounded
  intervals and confirm no restart or OOM.

- [ ] **Step 4: Validate lab02 UI and optional real endpoints**

  Repeat the lab01 browser checklist at `http://ai-lab02.esxi:1810/`. Run real
  RTSP/VLM smoke only when actual endpoint variables exist; otherwise retain
  `not_measured`.

- [ ] **Step 5: Commit dual-host evidence**

  Update the tracked progress document once, after both hosts are green. Record
  deployed SHA and archive digest, per-host health/resource/fixture/UI/free-disk
  evidence, rollback status, `not_measured` items, and no upstream push. Do not
  record secrets, URIs, tokens, request bodies, or raw logs.

  Commit:

  ```bash
  git add docs/2026-08-28-rtsp-responses-support_PROGRESS.md
  git commit -m "docs: record immutable dual-host lab acceptance"
  ```

  Explicitly state that this docs commit is later than the deployed runtime SHA.

---

### Task 5: Rollback drill, final regression, and handoff

**Files:**
- Modify: `deploy/ai-lab/README.md`
- Modify: `docs/2026-08-28-rtsp-responses-support_PROGRESS.md`
- Modify: `docs/superpowers/specs/2026-08-28-rtsp-responses-support-design.md`
- Modify: `docs/superpowers/specs/2026-08-28-ai-lab-simplified-deployment-design.md`

**Interfaces:**
- Consumes: two healthy host states, immutable published proofs, the deployed runtime SHA, and any previously capable lab01 release.
- Produces: rollback evidence, repository regression evidence, final measured/not-measured matrix, and operator handoff that distinguishes runtime SHA from later documentation HEAD.

- [ ] **Step 1: Exercise rollback without fabricating history**

  On lab01, if `previous` names a distinct capable SHA, run:

  ```bash
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw \
    ./deploy.sh rollback ai-lab01.esxi <previous-full-sha>
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw \
    ./deploy.sh verify ai-lab01.esxi
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw \
    ./deploy.sh rollback ai-lab01.esxi <final-runtime-sha>
  ```

  Verify health after each activation and prove both immutable proofs remain.
  If no distinct previous SHA exists, rollback to the current capable SHA and
  record version differentiation as `not_applicable`. Never create, copy, or
  delete state to manufacture a prior release.

- [ ] **Step 2: Re-run repository release gates**

  Run:

  ```bash
  ./scripts/local-ci.sh --tests
  backend/.venv/bin/python -m pytest -q deploy/ai-lab/tests/test_deploy_contract.py
  cd backend && uv run ruff check . && uv run ruff format --check . && uv run ty check .
  cd ../cli && uv run pytest -q
  cd ../web && pnpm test -- --run && pnpm typecheck && pnpm build
  cd .. && git diff --check
  ```

  Do not run `task lint`. Record the existing repository-wide type/format and
  macOS node-monitor baselines separately; do not misreport them as new feature
  failures or silently normalize them.

- [ ] **Step 3: Run final secret and artifact-boundary scans**

  Scan the feature merge base through final code HEAD plus the generated tar
  listing for private-key markers, cloud token forms, authorization headers,
  credential-bearing RTSP URIs, `.env`, `config.json`, `.git`, caches, venvs,
  raw request/response payloads, and base64 images. Inspect both containers only
  for structural configuration and confirm no API-key or RTSP-credential
  environment variable is present.

- [ ] **Step 4: Run final whole-branch review**

  Generate one review package from the feature merge base through code HEAD.
  The reviewer must inspect RTSP support, Responses support, immutable deploy
  state, acceptance packaging, secret boundaries, rollback, and the ledger's
  rulings/deferred items. One final fix wave and one scoped re-review are the
  maximum permitted by the SDD process.

- [ ] **Step 5: Update final documents and commit**

  Record:

  ```text
  implemented code commit
  deployed runtime SHA
  later docs-only HEAD
  lab01 and lab02 health/resource/UI evidence
  fixture RTSP perception/live results per host
  fixture Responses JSON/SSE/no-key/Bearer results per host
  real RTSP camera status
  real local VLM status
  rollback drill status
  immutable accumulation and 5 GiB stop behavior
  repository baseline limitations
  no upstream push
  ```

  Mark the simplified deployment spec `Implemented` only after Task 5 evidence
  is complete. Commit:

  ```bash
  git add deploy/ai-lab/README.md \
    docs/2026-08-28-rtsp-responses-support_PROGRESS.md \
    docs/superpowers/specs/2026-08-28-rtsp-responses-support-design.md \
    docs/superpowers/specs/2026-08-28-ai-lab-simplified-deployment-design.md
  git commit -m "docs: close immutable RTSP Responses lab validation"
  ```
