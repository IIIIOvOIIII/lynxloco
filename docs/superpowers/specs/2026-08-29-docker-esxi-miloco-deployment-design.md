# Docker ESXi Miloco Deployment Target Design

Status: Approved for minimal implementation on 2026-08-29.

## Goal

Deploy the modified Miloco RTSP/Responses branch to `docker.esxi` through the project `deploy.sh` flow, using a self-selected direct test port `1811`, without manual whole-repository transfer or production host mutation before CO/PAM approval.

## Scope

In scope:

- Add `docker.esxi` as a first-class deployment target in the existing immutable release controller.
- Keep the local immutable archive/receipt model unchanged: `dist/lab/<git_sha>/miloco-lab-<git_sha>.tar.gz` remains the controller artifact shape.
- Deploy the runtime container on `docker.esxi` with host networking and Miloco listening on port `1811`.
- Use `/opt/miloco` as the production deployment root:
  - `/opt/miloco/releases`
  - `/opt/miloco/state`
  - `/opt/miloco/deploy-state`
  - `/opt/miloco/incoming`
  - `/opt/miloco/control`
- Use production image tags distinct from lab tags:
  - runtime: `miloco:<sha>`
  - acceptance: `miloco-acceptance:<sha>`
  - candidates: `miloco-candidate:<sha>` and `miloco-acceptance-candidate:<sha>`
- Keep the same non-root runtime user `10001:10001`, read-only root filesystem, `no-new-privileges`, dropped capabilities, and state directory mode `0700`.
- Use production resource profile `2.0` CPU and `3072m` memory.
- Open an exact-SHA ITSM CO for `docker.esxi`, verify `state=Implement` and active PAM before production SSH, run deployment, verify health/status, and close the CO truthfully.

Out of scope:

- Apache reverse proxy, HTTPS, SmartDNS, public/internal FQDN, SSO, or URL publication.
- Real RTSP camera configuration.
- Real local VLM/Responses endpoint configuration.
- Secrets or Vault ref writes.
- Data migration from lab hosts.
- Restoring or operating unrelated lab services such as lab02 OpenObserve.

## Architecture

The existing `deploy.sh` stays the operator entrypoint. It gains a bounded host profile for `docker.esxi`, while `ai-lab01.esxi` and `ai-lab02.esxi` keep their current behavior.

The remote controller becomes profile-driven by host argument. At runtime it derives:

- deployment root;
- compose project;
- runtime/acceptance image names;
- service port and server URL;
- CPU/memory limits.

The packaged `compose.yaml` uses environment substitutions for image name, state directory, server port, and server URL. The Dockerfile healthcheck reads `MILOCO_SERVER__PORT`, so the same immutable archive can run on `1810` in lab and `1811` on `docker.esxi`.

## Safety rules

- `docker.esxi` must be rejected unless it is the exact host string.
- `docker.esxi` preflight must fail if port `1811` is occupied by an unrelated listener.
- If port `1811` is already occupied by the current Miloco deployment, preflight may continue only after proving the listener PID belongs to the current compose container and image.
- All deployment directories must be root-owned, non-symlink, and contained under the selected deployment root.
- Existing published releases remain immutable. Same-SHA publish reuses only a capable, already accepted release; it must not rebuild, retag, or rewrite proof.
- Rollback uses the existing verified-release activation path and must not delete release artifacts or state.
- Production deployment cannot start until ITSM `verify-deploy` passes for the exact final source SHA, `docker.esxi`, and `root`.

## Verification

Local verification:

- TDD regression for `docker.esxi` profile selection and bounded port/root/image behavior.
- Deployment contract suite.
- Shell syntax for `deploy.sh` and `deploy/ai-lab/remote-release.sh`.
- `git diff --check`.

Production verification after CO/PAM:

- `verify-deploy` with the exact source SHA and CO receipt.
- `./deploy.sh preflight docker.esxi`.
- `./deploy.sh deploy docker.esxi`.
- `./deploy.sh verify docker.esxi`.
- `./deploy.sh status docker.esxi`.
- HTTP health check for `http://docker.esxi:1811/health`.
- Bounded browser or HTTP smoke for `http://docker.esxi:1811/`.

## Rollback

If deployment fails before activation, the remote controller restores the prior current release or removes only the candidate container, matching the existing transition guard.

If deployment succeeds but post-change verification fails, run:

```bash
MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw ./deploy.sh rollback docker.esxi <previous-capable-sha>
MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw ./deploy.sh verify docker.esxi
MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw ./deploy.sh status docker.esxi
```

If there is no previous capable release on `docker.esxi`, stop the new Miloco compose project through the remote controller path and report the failed first deployment. Do not delete immutable release artifacts unless a separate cleanup change is approved.
