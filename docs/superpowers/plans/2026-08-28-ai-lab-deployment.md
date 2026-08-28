# Miloco ai-lab Dual-Host Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a content-addressed Miloco lab release from the completed RTSP and OpenAI Responses branch, deploy it sequentially to `ai-lab01.esxi` and `ai-lab02.esxi`, and produce rollback and host-specific acceptance evidence without copying the repository or exposing credentials.

**Architecture:** The workstation builds the Web assets and Python wheels from the exact clean Git commit. `deploy.sh` packages only those release artifacts, the ONNX model archive, a pinned Docker build definition, and bounded acceptance fixtures; it streams that checksum-verified artifact to `/opt/miloco-lab/releases/<git-sha>` on the selected host. Each Linux host builds the amd64 runtime and acceptance images, runs the production container with host networking and a persistent owner-only state directory, and automatically restores the previous image if health validation fails.

**Tech Stack:** Bash, Git, `uv`, `pnpm`, Python 3.12, Docker Engine 26+, Docker Compose 2.26+, FastAPI/Uvicorn, PyAV, pytest, SHA-256 manifests, SSH.

**Spec:** `docs/superpowers/specs/2026-08-28-rtsp-responses-support-design.md`

## Global Constraints

- Target only `ai-lab01.esxi` and `ai-lab02.esxi`; both are lab hosts exempt from CO and PAM.
- Deploy `linux/amd64`; reject every other remote architecture.
- Use the exact clean Git commit as release identity. Never deploy uncommitted files or silently rebuild a different commit.
- Transfer only built wheels, model archive, deployment control files, and bounded acceptance fixtures. Never copy the worktree, `.git`, local config, caches, logs, credentials, or arbitrary source directories.
- Use `deploy.sh` for preflight, build, transfer, activation, verification, status, and rollback.
- Base image is the official multi-architecture `python:3.12-slim-bookworm` image pinned to index digest `sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579`.
- The runtime container runs as UID/GID `10001:10001`, uses `network_mode: host`, listens on `0.0.0.0:1810`, and stores state only under `/opt/miloco-lab/state` with owner-only permissions.
- Lab limits are host-specific: `ai-lab01.esxi` gets `3.0` CPUs and `3072m`; `ai-lab02.esxi` gets `1.25` CPUs and `1536m`.
- Never put RTSP credentials, API keys, server tokens, or complete credential-bearing URIs in Git, artifacts, command arguments printed to logs, Compose files, image layers, acceptance output, or progress documents.
- Synthetic fixture acceptance proves production wiring only. Without `MILOCO_RTSP_TEST_URL` or a real Responses VLM endpoint, real-camera and real-VLM results remain `not_measured`.
- Deploy sequentially: lab01 preflight/build/acceptance/activation/health first; start lab02 only after lab01 is green.
- A failed activation must automatically restore the previously recorded image on that host. A failed lab02 rollout must not disturb the healthy lab01 deployment.
- Do not push to `XiaoMi/xiaomi-miloco`; the upstream remote is read-only for this work.

---

### Task 1: Lock the deployment contract and artifact allowlist

**Files:**
- Create: `deploy/ai-lab/tests/test_deploy_contract.py`
- Create: `deploy/ai-lab/artifact-files.txt`
- Create: `deploy/ai-lab/README.md`

**Interfaces:**
- Consumes: exact Git SHA from `git rev-parse HEAD`; built files from `dist/`; fixture files already committed under `backend/miloco/tests/integration`, `backend/miloco/tests/fixtures/rtsp`, and `scripts/`.
- Produces: `artifact-files.txt`, the sole allowlist accepted by `deploy.sh`; test helpers that invoke the deployment CLI with stubbed remote commands.

- [ ] **Step 1: Write failing allowlist and CLI contract tests**

  Add tests that require:

  ```python
  EXPECTED_COMMANDS = {"build", "preflight", "deploy", "verify", "status", "rollback"}
  ALLOWED_HOSTS = {"ai-lab01.esxi", "ai-lab02.esxi"}
  FORBIDDEN_PARTS = {".git", ".env", "config.json", ".venv", "node_modules", "__pycache__"}
  ```

  The tests must assert that `deploy.sh --help` exposes exactly those operations, unknown hosts exit `2` before SSH, a dirty worktree exits `3` before build/transfer, and the artifact manifest contains no forbidden path component. Stub `ssh`, `tar`, and the build command through a temporary `PATH`; record arguments without logging environment values.

- [ ] **Step 2: Run the tests and verify RED**

  Run:

  ```bash
  backend/.venv/bin/python -m pytest -q deploy/ai-lab/tests/test_deploy_contract.py
  ```

  Expected: failure because `deploy.sh`, the allowlist, and its exit-code contract do not yet exist.

- [ ] **Step 3: Write the exact artifact allowlist and operator documentation**

  `artifact-files.txt` must allow only:

  ```text
  Dockerfile
  compose.yaml
  container-entrypoint.sh
  remote-release.sh
  acceptance/
  requirements/backend.txt
  requirements/cli.txt
  requirements/acceptance.txt
  wheels/miloco-*.whl
  wheels/miloco_cli-*.whl
  wheels/miloco_miot-*-manylinux_2_28_x86_64.whl
  models/miloco-models-*.tar.gz
  release.json
  SHA256SUMS
  ```

  `README.md` must document the two hosts, exact six commands, automatic rollback behavior, state path, port `1810`, resource limits, and the `not_measured` rule for absent real endpoints. It must not include credential examples with literal secret values.

- [ ] **Step 4: Commit the contract skeleton**

  ```bash
  git add deploy/ai-lab/tests/test_deploy_contract.py deploy/ai-lab/artifact-files.txt deploy/ai-lab/README.md
  git commit -m "test(deploy): define ai-lab release contract"
  ```

---

### Task 2: Build a non-root runtime and isolated acceptance image

**Files:**
- Create: `deploy/ai-lab/Dockerfile`
- Create: `deploy/ai-lab/compose.yaml`
- Create: `deploy/ai-lab/container-entrypoint.sh`
- Create: `deploy/ai-lab/acceptance/pytest.ini`
- Modify: `deploy/ai-lab/tests/test_deploy_contract.py`

**Interfaces:**
- Consumes: the three Linux-compatible wheels, model tarball, and acceptance bundle assembled by Task 3.
- Produces: runtime image `miloco-lab:<git-sha>` and test-only image `miloco-lab-acceptance:<git-sha>`; Compose variables `MILOCO_RELEASE_SHA`, `MILOCO_CPU_LIMIT`, and `MILOCO_MEMORY_LIMIT`.

- [ ] **Step 1: Extend tests for image and Compose invariants**

  Parse the files as text/YAML and assert all of the following:

  ```text
  FROM python:3.12-slim-bookworm@sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579
  user 10001:10001
  network_mode: host
  read_only: true
  no-new-privileges:true
  MILOCO_HOME=/var/lib/miloco
  MILOCO_DIRECTORIES__MODELS=/opt/miloco/models
  MILOCO_SERVER__HOST=0.0.0.0
  MILOCO_SERVER__PORT=1810
  MILOCO_SERVER__URL=http://127.0.0.1:1810
  health URL http://127.0.0.1:1810/health
  ```

  Also assert that runtime layers never copy `acceptance/`, no API key/token variable appears in Dockerfile or Compose, `/tmp` is a bounded tmpfs, and only `/var/lib/miloco` is writable/persistent.

- [ ] **Step 2: Run the image-contract tests and verify RED**

  Run the Task 1 pytest command. Expected: failures for absent Dockerfile, Compose, entrypoint, and pytest configuration.

- [ ] **Step 3: Implement the multi-stage Dockerfile**

  Build these stages:

  ```dockerfile
  FROM python:3.12-slim-bookworm@sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579 AS runtime
  # install only ca-certificates and the wheel runtime; create UID/GID 10001
  # install hash-pinned external dependencies, then local miloco_miot, miloco,
  # and miloco_cli wheels with --no-deps and --no-cache-dir
  # extract the model tarball into /opt/miloco/models
  # set MILOCO_HOME, models path, host, port, and Python unbuffered variables
  # HEALTHCHECK calls urllib against /health
  # USER 10001:10001; ENTRYPOINT container-entrypoint.sh

  FROM runtime AS acceptance
  # install hash-pinned acceptance dependencies only in this target and copy
  # the bounded acceptance tree; default command runs the selected fixture tests
  ```

  `container-entrypoint.sh` must umask `077`, refuse a non-writable or group/world-accessible state directory, and `exec miloco-backend` without echoing its environment.

- [ ] **Step 4: Implement Compose runtime isolation**

  `compose.yaml` must define one service named `miloco`, image `miloco-lab:${MILOCO_RELEASE_SHA}`, `network_mode: host`, `restart: unless-stopped`, `read_only: true`, `security_opt: [no-new-privileges:true]`, capability drop `ALL`, host state bind mount `/opt/miloco-lab/state:/var/lib/miloco`, tmpfs `/tmp:size=256m,mode=1777`, and the two resource variables. It must not contain a `build:` section or secrets.

- [ ] **Step 5: Run contract/static checks and commit**

  ```bash
  backend/.venv/bin/python -m pytest -q deploy/ai-lab/tests/test_deploy_contract.py
  bash -n deploy/ai-lab/container-entrypoint.sh
  git diff --check
  git add deploy/ai-lab
  git commit -m "feat(deploy): add isolated ai-lab runtime"
  ```

---

### Task 3: Implement content-addressed build, transfer, activation, and rollback

**Files:**
- Create: `deploy.sh`
- Create: `deploy/ai-lab/remote-release.sh`
- Modify: `deploy/ai-lab/tests/test_deploy_contract.py`
- Modify: `deploy/ai-lab/README.md`

**Interfaces:**
- Consumes: `artifact-files.txt`, `scripts/build.sh`, exact clean Git SHA, SSH aliases for the two labs.
- Produces: local `dist/lab/<sha>/miloco-lab-<sha>.tar.gz`; remote release `/opt/miloco-lab/releases/<sha>`; state files `/opt/miloco-lab/deploy-state/current` and `previous`.

- [ ] **Step 1: Add RED tests for preflight, checksum, and rollback state transitions**

  Stub the remote shell and assert:

  ```text
  preflight checks Docker >=26, Compose >=2.26, linux/amd64, port 1810, >=5 GiB free
  build rejects dirty worktree and records full 40-character Git SHA
  transfer expands only under /opt/miloco-lab/releases/<sha>
  remote verifies SHA256SUMS before docker build
  activation writes previous before current
  failed health restores previous and returns nonzero
  rollback rejects unknown/non-built SHA
  deploy never invokes scp, rsync of '.', or tar of the repository root
  ```

- [ ] **Step 2: Run focused tests and verify RED**

  Run the Task 1 pytest command. Expected: deployment state-machine assertions fail.

- [ ] **Step 3: Implement local `deploy.sh build`**

  The command must:

  1. confirm `git status --porcelain` is empty and capture `git rev-parse HEAD`;
  2. run `./scripts/build.sh --packages web,miloco-miot,miloco,miloco-cli`;
  3. select exactly one Miloco wheel, one CLI wheel, one Linux x86_64 MIoT wheel, and one model archive;
  4. export hash-pinned external requirements from the committed locks: backend runtime with `uv export --locked --no-dev --no-emit-workspace`, CLI runtime with the same flags from `cli/`, and backend acceptance dependencies with `uv export --locked --no-emit-workspace`; write them as `requirements/backend.txt`, `requirements/cli.txt`, and `requirements/acceptance.txt`;
  5. copy only those files plus `deploy/ai-lab` control files and the selected RTSP/Responses tests, fixture server, fixture media, and smoke scripts into a temporary staging directory;
  6. write `release.json` with schema `1`, full Git SHA, UTC build time, platform `linux/amd64`, and artifact filenames;
  7. generate `SHA256SUMS`, validate every staged path against `artifact-files.txt`, then write `dist/lab/<sha>/miloco-lab-<sha>.tar.gz` atomically.

  The command must use `mktemp -d`, remove the temporary directory on every exit, and never inherit generic Omni/Responses/RTSP credential variables into build subprocesses.

- [ ] **Step 4: Implement remote preflight and artifact transfer**

  `deploy.sh preflight HOST` must reject hosts outside the allowlist and remotely verify architecture, Docker/Compose versions, disk, memory, port `1810`, and that `/opt/miloco-lab` is not a symlink. Port `1810` may be free or owned by the already recorded `miloco-lab` container during an upgrade; any other listener is a hard failure. `deploy.sh deploy HOST` must stream the single release tar through SSH stdin into a newly created exact release directory, never use `scp`, and call `remote-release.sh` with the expected full SHA.

- [ ] **Step 5: Implement remote image build and automatic rollback**

  `remote-release.sh` must:

  1. verify owner/root, release path, `release.json`, and every SHA-256 entry before build;
  2. build `--platform linux/amd64 --target runtime -t miloco-lab:<sha>` and `--target acceptance -t miloco-lab-acceptance:<sha>`;
  3. run acceptance image tests before changing the service;
  4. create `/opt/miloco-lab/state` as `10001:10001` mode `0700`;
  5. assign host-specific CPU/memory values from an explicit case statement;
  6. save current SHA as previous, run Compose with the new SHA, and wait up to 120 seconds for container health plus `GET /health` status 200;
  7. on failure, collect only status/sanitized tail logs, restore the previous image when present, and exit nonzero;
  8. on success, atomically write current SHA and retain the prior two release directories/images for rollback.

  `rollback HOST SHA` must require that exact SHA's verified release and image exist, activate it through the same health gate, and never delete state.

- [ ] **Step 6: Run deployment contract tests and dry-run help/status paths**

  ```bash
  backend/.venv/bin/python -m pytest -q deploy/ai-lab/tests/test_deploy_contract.py
  bash -n deploy.sh deploy/ai-lab/remote-release.sh deploy/ai-lab/container-entrypoint.sh
  ./deploy.sh --help
  ./deploy.sh status ai-lab01.esxi
  ./deploy.sh status ai-lab02.esxi
  git diff --check
  ```

  `status` is read-only and may report `not_deployed`; it must not create remote directories.

- [ ] **Step 7: Commit the deployment controller**

  ```bash
  git add deploy.sh deploy/ai-lab
  git commit -m "feat(deploy): add rollback-safe lab rollout"
  ```

---

### Task 4: Make packaged acceptance reproduce RTSP and Responses contracts

**Files:**
- Modify: `deploy.sh`
- Modify: `deploy/ai-lab/Dockerfile`
- Modify: `deploy/ai-lab/tests/test_deploy_contract.py`
- Modify: `scripts/rtsp-view-smoke.sh`
- Modify: `scripts/responses-vlm-smoke.sh`
- Modify: `backend/miloco/tests/integration/test_rtsp_live_view.py`
- Modify: `backend/miloco/tests/integration/test_responses_perception.py`
- Create during artifact build only: `acceptance/integration/`, `acceptance/fixtures/rtsp/`, `acceptance/scripts/`

**Interfaces:**
- Consumes: committed production integration tests and fixture assets; installed wheel code inside the acceptance image.
- Produces: host-local fixture proof for RTSP perception, RTSP WebSocket live view, and Responses non-stream/SSE/no-key/Bearer contracts before service activation.

- [ ] **Step 1: Add RED tests for the bounded acceptance manifest**

  Assert the build staging function copies exactly:

  ```text
  test_rtsp_perception.py
  test_rtsp_live_view.py
  test_responses_perception.py
  responses_fixture_server.py
  fixtures/rtsp/*
  scripts/rtsp-view-smoke.sh
  scripts/responses-vlm-smoke.sh
  ```

  Assert no `conftest.py`, user config, credential file, unrelated test tree, or Git metadata enters the acceptance layer. Verify smoke scripts remain executable. Add regression tests requiring both Python-based smoke scripts to honor an executable `MILOCO_SMOKE_PYTHON` override while preserving their existing repository-venv/`uv` fallback.

- [ ] **Step 2: Run the contract test and verify RED**

  Run the deployment pytest. Expected: failure until the staging map and acceptance image paths are exact.

- [ ] **Step 3: Make the two Python smoke scripts package-runtime aware**

  `rtsp-view-smoke.sh` and `responses-vlm-smoke.sh` must use `MILOCO_SMOKE_PYTHON` only when it names an executable file; otherwise they retain the current controlled repository fallback. The override value must never be echoed, shell-evaluated, or accepted as multiple words. The acceptance image sets it to `/usr/local/bin/python`.

- [ ] **Step 4: Wire the selected tests to the installed wheel**

  The acceptance stage must set `PYTHONPATH=/acceptance/integration`, `MILOCO_CONFIG_SEARCH_PATH=/tmp/miloco-acceptance-config`, and clear generic/Responses API-key variables. Its default command must run:

  ```bash
  python -m pytest -q \
    /acceptance/integration/test_rtsp_perception.py \
    /acceptance/integration/test_rtsp_live_view.py::test_fixture_perception_and_uvicorn_live_view_share_one_rtsp_session \
    /acceptance/integration/test_responses_perception.py \
    -k 'not smoke'
  ```

  Preserve each test's production `miloco.*` imports, real PyAV fixture decode, real Uvicorn WebSocket client, real localhost `httpx`, and current parser/breaker assertions. The separately tested smoke entrypoints remain available for optional real endpoints, but repository-layout subprocess tests are deselected inside the installed-wheel image. Do not replace production calls with mocks of the public result.

- [ ] **Step 5: Prove the acceptance image locally or on lab01 without activation**

  If the workstation Docker daemon is unavailable, use `deploy.sh build`, transfer to lab01, and invoke the remote `build-and-accept` path without Compose activation. Expected: the three selected modules pass from the installed wheels. A failure blocks activation.

- [ ] **Step 6: Commit acceptance packaging**

  ```bash
  git add deploy.sh deploy/ai-lab
  git commit -m "test(deploy): package lab acceptance fixtures"
  ```

---

### Task 5: Deploy and validate `ai-lab01.esxi`

**Files:**
- Modify: `docs/2026-08-28-rtsp-responses-support_PROGRESS.md`

**Interfaces:**
- Consumes: exact release archive from Task 4 and read-only host preflight.
- Produces: running lab01 Miloco container, recorded SHA/health/fixture evidence, rollback target.

- [ ] **Step 1: Record the release SHA and run preflight**

  ```bash
  ./deploy.sh build
  ./deploy.sh preflight ai-lab01.esxi
  ```

  Confirm `linux/amd64`, Docker/Compose minimums, free port `1810`, resource limits, and no unrelated container mutation.

- [ ] **Step 2: Deploy lab01 through the verified artifact path**

  ```bash
  ./deploy.sh deploy ai-lab01.esxi
  ```

  Expected: checksum verification, both image builds, selected fixture tests, activation, container health, and `/health` 200 all succeed. If any stage fails, confirm automatic restoration and stop before lab02.

- [ ] **Step 3: Verify packaged runtime and browser surface**

  Run:

  ```bash
  ./deploy.sh verify ai-lab01.esxi
  ./deploy.sh status ai-lab01.esxi
  ```

  Verify exact running SHA, non-root UID, owner-only state, health, root SPA, camera management API authentication, model profile protocol field, and absence of credentials in container inspect/log output. Use the browser against `http://ai-lab01.esxi:1810/` to confirm the dashboard, RTSP camera controls, model protocol selector, and generic watch route render without console-blocking errors.

- [ ] **Step 4: Record real-endpoint boundaries**

  If a real RTSP URL is present in the operator environment, run `scripts/rtsp-smoke.sh` and then `scripts/rtsp-view-smoke.sh` against the temporary camera, letting cleanup delete it. If a real local Responses VLM URL/model is present, run `scripts/responses-vlm-smoke.sh`. Otherwise record each as `not_measured`; do not invent credentials or persist a synthetic camera/profile.

- [ ] **Step 5: Update progress with provisional lab01 evidence**

  Record timestamp, exact SHA, fixture totals, container health, UI result, resource snapshot, rollback status, and every `not_measured` item. Do not record tokens, keys, RTSP URIs, or generated server tokens. Keep this documentation change uncommitted until lab02 has deployed the same already-built runtime SHA; a docs-only commit between hosts would otherwise make `HEAD` differ from the artifact identity.

---

### Task 6: Deploy and validate `ai-lab02.esxi`

**Files:**
- Modify: `docs/2026-08-28-rtsp-responses-support_PROGRESS.md`

**Interfaces:**
- Consumes: the exact same release SHA proven on lab01.
- Produces: independently healthy lab02 deployment using the constrained resource profile and comparative evidence without changing lab01.

- [ ] **Step 1: Enforce same-artifact rollout and run preflight**

  Confirm local HEAD and the built `release.json` SHA equal lab01's current SHA. Then run:

  ```bash
  ./deploy.sh preflight ai-lab02.esxi
  ```

  Reject rebuild drift. Confirm the `1.25` CPU/`1536m` profile and port `1810` availability.

- [ ] **Step 2: Deploy lab02**

  ```bash
  ./deploy.sh deploy ai-lab02.esxi
  ```

  Expected: the same checksum, images, acceptance tests, health gate, and rollback semantics as lab01. A lab02 failure rolls back only lab02.

- [ ] **Step 3: Verify runtime and resource-constrained behavior**

  ```bash
  ./deploy.sh verify ai-lab02.esxi
  ./deploy.sh status ai-lab02.esxi
  ```

  Record exact SHA, health, restart count, memory/CPU snapshot, owner-only state, SPA/API availability, and fixture results. Confirm no OOM/restart during acceptance and a five-minute idle observation.

- [ ] **Step 4: Run the same browser and optional real-endpoint checks**

  Validate `http://ai-lab02.esxi:1810/` with the same browser checklist. Run real RTSP/VLM smokes only when the corresponding endpoint variables are actually available; otherwise retain `not_measured`.

- [ ] **Step 5: Commit dual-host evidence**

  ```bash
  git add docs/2026-08-28-rtsp-responses-support_PROGRESS.md
  git commit -m "docs: record dual-host lab acceptance"
  ```

---

### Task 7: Final regression, rollback drill, and handoff

**Files:**
- Modify: `deploy/ai-lab/README.md`
- Modify: `docs/2026-08-28-rtsp-responses-support_PROGRESS.md`
- Modify: `docs/superpowers/specs/2026-08-28-rtsp-responses-support-design.md`

**Interfaces:**
- Consumes: both healthy host states, at least one previous content-addressed release on lab01 or a no-op current-SHA rollback target.
- Produces: reproducible operator handoff, verified rollback procedure, final measured/not-measured matrix.

- [ ] **Step 1: Exercise rollback without touching persistent data**

  On lab01, activate the recorded previous verified release when one exists, prove health, then reactivate the final SHA and prove health again. If no previous release exists, run rollback to the current verified SHA and record that destructive rollback differentiation was `not_applicable`; never synthesize or delete state to manufacture a prior version.

- [ ] **Step 2: Re-run repository release gates**

  ```bash
  ./scripts/local-ci.sh --tests
  cd backend && uv run ruff check . && uv run ruff format --check . && uv run ty check .
  cd ../cli && uv run pytest -q
  cd ../web && pnpm test -- --run && pnpm typecheck && pnpm build
  git diff --check
  ```

  Record the existing repository-wide type/format and macOS node-monitor baselines separately from feature/lab pass results. Never run the mutating `task lint`.

- [ ] **Step 3: Run final secret and artifact-boundary scans**

  Scan `e900529..HEAD` and the generated tar listing for private-key markers, cloud token patterns, credential-bearing RTSP URIs, `.env`, `config.json`, caches, Git metadata, and raw image/base64 trace payloads. Verify both remote `docker inspect` outputs contain no API key or RTSP credential variables.

- [ ] **Step 4: Update final documents**

  The design and progress documents must report:

  ```text
  implemented commit
  lab01 deployed SHA and health
  lab02 deployed SHA and health
  fixture RTSP perception/live results per host
  fixture Responses JSON/SSE/no-key/Bearer results per host
  real RTSP camera status
  real local VLM status
  rollback drill status
  repository baseline limitations
  no upstream push
  ```

  Do not upgrade a `not_measured` item based on fixture evidence. Explicitly distinguish the deployed runtime SHA from any later docs-only HEAD created by Tasks 6–7.

- [ ] **Step 5: Commit final handoff**

  ```bash
  git add deploy/ai-lab/README.md docs/2026-08-28-rtsp-responses-support_PROGRESS.md docs/superpowers/specs/2026-08-28-rtsp-responses-support-design.md
  git commit -m "docs: close RTSP Responses lab validation"
  ```
