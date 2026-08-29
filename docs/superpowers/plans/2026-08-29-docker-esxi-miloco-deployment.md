# Docker ESXi Miloco Deployment Target Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a minimal, repeatable `docker.esxi` production deployment target for the completed Miloco RTSP/Responses branch.

**Architecture:** Keep the existing immutable archive/receipt and remote transaction model. Make the host-specific remote deployment values profile-driven so lab hosts continue on `/opt/miloco-lab:1810`, while `docker.esxi` runs under `/opt/miloco:1811` with distinct image tags.

**Tech Stack:** Bash deployment controller, Docker Compose, pytest deployment-contract tests, ITSM CO/PAM production gate.

**Spec:** `docs/superpowers/specs/2026-08-29-docker-esxi-miloco-deployment-design.md`

## Global Constraints

- `docker.esxi` is the only new production host.
- Production direct port is exactly `1811`.
- Production deployment root is exactly `/opt/miloco`.
- Production runtime image tag is `miloco:<sha>`.
- Production acceptance image tag is `miloco-acceptance:<sha>`.
- Production candidate tags are `miloco-candidate:<sha>` and `miloco-acceptance-candidate:<sha>`.
- Production compose project is exactly `miloco`.
- Production resource profile is exactly `2.0` CPU and `3072m` memory.
- Local immutable archive path remains `dist/lab/<sha>/miloco-lab-<sha>.tar.gz`.
- No production SSH or host mutation before ITSM `state=Implement` and active PAM.
- No Apache, HTTPS, SmartDNS, FQDN, secret, RTSP camera, or real local VLM configuration in this change.

---

### Task 1: Add host-profile deployment support

**Files:**
- Modify: `deploy.sh`
- Modify: `deploy/ai-lab/remote-release.sh`
- Modify: `deploy/ai-lab/compose.yaml`
- Modify: `deploy/ai-lab/Dockerfile`
- Modify: `deploy/ai-lab/tests/test_deploy_contract.py`
- Create: `docs/2026-08-29-docker-esxi-deployment_PROGRESS.md`

**Interfaces:**
- Consumes: current immutable archive builder, receipt reader, remote transaction, status, verify, and rollback operations.
- Produces: `./deploy.sh preflight|deploy|verify|status|rollback docker.esxi`, backed by a remote profile using `/opt/miloco`, port `1811`, compose project `miloco`, and production image names.

- [ ] **Step 1: Write failing profile tests**

Add executable tests that copy the real controller into pytest temp directories and prove:

```python
def test_deploy_streams_archive_to_docker_esxi_with_production_profile(tmp_path):
    # first SSH installs controller under /opt/miloco/control/<digest>/remote-release.sh
    # second SSH preflights docker.esxi through the installed controller
    # third SSH streams exactly one immutable archive to transaction docker.esxi
    # every SSH call uses MILOCO_SSH_IDENTITY and no scp/sftp/rsync whole-repo transfer
    ...
```

Add a remote-controller harness test that calls the real profile code and proves:

```python
def test_remote_docker_esxi_profile_uses_port_1811_and_production_names(tmp_path):
    # docker.esxi derives /opt/miloco, port 1811, compose project miloco,
    # runtime image miloco:<sha>, acceptance image miloco-acceptance:<sha>,
    # and resource profile cpu=2.0/memory=3072m.
    ...
```

- [ ] **Step 2: Run focused tests and confirm RED**

Run:

```bash
backend/.venv/bin/python -m pytest -q deploy/ai-lab/tests/test_deploy_contract.py::test_deploy_streams_archive_to_docker_esxi_with_production_profile deploy/ai-lab/tests/test_deploy_contract.py::test_remote_docker_esxi_profile_uses_port_1811_and_production_names
```

Expected: fail because `docker.esxi` is not allowed and the remote script hard-codes lab root/port/image names.

- [ ] **Step 3: Implement minimal profile support**

In `deploy.sh`:

- add `docker.esxi` to host validation;
- derive remote root per host:
  - lab hosts: `/opt/miloco-lab`;
  - production: `/opt/miloco`;
- install and call the digest-addressed remote controller under the selected root's `control/` directory;
- keep local build and receipt paths unchanged.

In `deploy/ai-lab/remote-release.sh`:

- initialize lab defaults for backward-compatible harness behavior;
- configure the selected host profile at the start of `main`;
- use the selected profile in preflight, listener ownership, compose wrapper, HTTP health probe, image build/tag/reuse, status, rollback, and path safety checks.

In `deploy/ai-lab/compose.yaml`:

- replace hard-coded image, state directory, port, and server URL with environment-substituted values that default to the lab values.

In `deploy/ai-lab/Dockerfile`:

- make the healthcheck read `MILOCO_SERVER__PORT`, defaulting to `1810`.

- [ ] **Step 4: Run focused GREEN**

Run:

```bash
backend/.venv/bin/python -m pytest -q deploy/ai-lab/tests/test_deploy_contract.py::test_deploy_streams_archive_to_docker_esxi_with_production_profile deploy/ai-lab/tests/test_deploy_contract.py::test_remote_docker_esxi_profile_uses_port_1811_and_production_names
```

Expected: pass.

- [ ] **Step 5: Run deployment contract and syntax gates**

Run:

```bash
backend/.venv/bin/python -m pytest -q deploy/ai-lab/tests/test_deploy_contract.py
bash -n deploy.sh deploy/ai-lab/remote-release.sh
git diff --check
```

Expected: deployment contract passes and shell syntax/diff checks pass.

- [ ] **Step 6: Commit**

Commit the implementation:

```bash
git add deploy.sh deploy/ai-lab/remote-release.sh deploy/ai-lab/compose.yaml deploy/ai-lab/Dockerfile deploy/ai-lab/tests/test_deploy_contract.py docs/superpowers/specs/2026-08-29-docker-esxi-miloco-deployment-design.md docs/superpowers/plans/2026-08-29-docker-esxi-miloco-deployment.md docs/2026-08-29-docker-esxi-deployment_PROGRESS.md
git commit -m "feat(deploy): add docker esxi miloco target"
```

### Task 2: Open CO and deploy exact SHA

**Files:**
- Read: committed source tree
- Write outside repo as needed: ITSM CO payload/receipt files under a safe temporary directory

**Interfaces:**
- Consumes: committed exact SHA from Task 1.
- Produces: CO number, active PAM gate, deployed `docker.esxi:1811`, verification, and closed CO.

- [ ] **Step 1: Build exact immutable archive**

Run from a clean tree:

```bash
./deploy.sh build
```

Expected: one new immutable archive for the final SHA under `dist/lab/<sha>/`.

- [ ] **Step 2: Create CO**

Build an English ITSM payload for service `Miloco`, host `docker.esxi`, target user `root`, exact SHA, `/opt/miloco`, and port `1811`. Create the CO with a receipt file.

- [ ] **Step 3: Wait for approval gate**

Poll until `state=Implement` and `pam_status=active`. If AI denies, close as `Not Executed`, revise once, and retry once. If `AI+Lynx`, stop for user approval.

- [ ] **Step 4: Verify deploy gate**

Run:

```bash
python3 scripts/itsm_co.py verify-deploy <CO> --payload-file <payload> --receipt-file <receipt> --expected-sha <sha> --host docker.esxi --user root
```

Expected: success.

- [ ] **Step 5: Deploy and verify**

Run:

```bash
MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw ./deploy.sh preflight docker.esxi
MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw ./deploy.sh deploy docker.esxi
MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw ./deploy.sh verify docker.esxi
MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw ./deploy.sh status docker.esxi
```

Then verify:

```bash
curl --silent --show-error --fail --max-time 5 http://docker.esxi:1811/health
```

Expected: healthy runtime on exact SHA and HTTP health success.

- [ ] **Step 6: Close CO**

Close successfully only after deployment and verification pass. If deployment does not execute, close `Not Executed`; if it executes and fails unrecovered, close `Failed` with exact facts.
