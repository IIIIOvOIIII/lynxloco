# Miloco AI-lab release contract

This directory defines the release payload and operator contract for an
isolated Miloco deployment. The built-in target names are reserved examples:
`miloco-staging-a.example.com`, `miloco-staging-b.example.com`, and
`miloco-production.example.com`. Real hostnames, SSH identity paths, or private
deployment details must be supplied by the operator environment and must not be
committed to the public repository.

`artifact-files.txt` is the sole release-payload allowlist. A release is built
from the current clean Git `HEAD`, with its exact SHA recorded in
`release.json`. It carries no repository metadata, local environments, runtime
configuration, or credentials. The atomic mode-0444 receipt binds the full Git
SHA, archive SHA-256, controller SHA-256, allowlist SHA-256, and exact
repository-relative archive path. `deploy` recomputes those values before any
remote connection, so an ignored or replaced archive cannot be endorsed.

## Operator commands

Run the canonical controller from the repository root:

```bash
./deploy.sh build
MILOCO_SSH_IDENTITY=/absolute/path/to/lab-identity ./deploy.sh preflight miloco-staging-a.example.com
MILOCO_SSH_IDENTITY=/absolute/path/to/lab-identity ./deploy.sh deploy miloco-staging-a.example.com
MILOCO_SSH_IDENTITY=/absolute/path/to/lab-identity ./deploy.sh verify miloco-staging-a.example.com
MILOCO_SSH_IDENTITY=/absolute/path/to/lab-identity ./deploy.sh status miloco-staging-a.example.com
MILOCO_SSH_IDENTITY=/absolute/path/to/lab-identity ./deploy.sh rollback miloco-staging-a.example.com <full-40-character-sha>
```

Use the same positional command shape with `miloco-staging-b.example.com`, or the equivalent
`--host miloco-staging-b.example.com` form. `build` refuses a dirty worktree and does not need
an SSH identity. Every remote command requires `MILOCO_SSH_IDENTITY`: an
absolute regular file, not a symlink, owned by the current user, with no group
or other permission bits. The controller passes that path using SSH
`IdentitiesOnly=yes`; it never reads or prints its contents.

To deploy against real infrastructure, inject the private target names at run
time instead of editing the script:

```bash
export MILOCO_DEPLOY_STAGING_A_HOST=staging-a.internal.example
export MILOCO_DEPLOY_STAGING_B_HOST=staging-b.internal.example
export MILOCO_DEPLOY_PRODUCTION_HOST=miloco.internal.example
export MILOCO_SSH_IDENTITY=/absolute/path/to/private-ssh-key

./deploy.sh preflight "$MILOCO_DEPLOY_PRODUCTION_HOST"
./deploy.sh deploy "$MILOCO_DEPLOY_PRODUCTION_HOST"
```

Keep those environment values in your local shell, CI secret store, password
manager, or deployment orchestrator. Do not write real hostnames, key paths,
tokens, API keys, passwords, RTSP URLs, or credential inventories into this
repository.

Every remote operation also requires both controllers to be tracked and the
whole worktree to be the exact clean `HEAD`. `preflight` validates the selected
host and release before any remote change. `deploy` restores the last
known-good release when post-deploy verification fails; an operator can also
run `rollback`. A failed restoration is reported as `rollback_failed` with
exit code `70`.

The two-host rollout is one build and two deployments: run `./deploy.sh build`
once, preserve its exact SHA-addressed archive and receipt unchanged, then
deploy that same full 40-character SHA first to `miloco-staging-a.example.com` and then to
`miloco-staging-b.example.com`. Do not rebuild, replace the archive, or create another SHA
between the two host deployments.

`./deploy.sh --help` provides one machine-readable authoritative line:
`Operations: build preflight deploy verify status rollback`.

## Immutable release behavior

Published releases accumulate immutably under
`/opt/miloco-lab/releases/<sha>`. A SHA is considered new only when its release
directory, artifact receipt, acceptance marker, runtime canonical tag
`miloco-lab:<sha>`, and acceptance canonical tag
`miloco-lab-acceptance:<sha>` are all absent. Docker observation uncertainty is
a fail-closed error, never permission to publish.

For an existing SHA, the controller receives the supplied archive only into a
private temporary input and compares its receipt. It then requires complete
release, artifact, marker, and image-ID proof to be capable. A capable SHA is
reused with no build, tagging, marker, publication, or proof deletion. An
invalid or uncertain SHA fails without repair, replacement, or cleanup. To
produce another release, make a new clean Git commit and deploy its new SHA.

A new SHA is received and verified under one host-wide lock. Its archive is
checked before extraction; unsafe, duplicate, linked, special, escaping, and
non-allowlisted members are rejected. The verified release and receipt are
published atomically, isolated candidate runtime and acceptance images are
built, and fixture acceptance runs before the candidate IDs receive canonical
tags. The acceptance marker then binds the archive digest and both canonical
image IDs.

Only unpublished transaction-local material can be removed automatically:
incoming temporary archives, extraction staging directories, temporary
atomic-write files, isolated candidate tags that never received acceptance
proof, and a failed candidate container. Activation failure may restore the
former capable service, but it preserves the accepted release directory,
canonical image pair, receipt, and marker for the attempted SHA.

The existing preflight requires at least 5 GiB free disk before a transfer or
build. It is a hard non-destructive stop: the controller does not reclaim
published history. Any future history cleanup requires a separately governed
maintenance design, explicit authorization, dedicated tests, and review.

## Runtime and read-only boundaries

Persistent application state is `/opt/miloco-lab/state`; deployment state is
`/opt/miloco-lab/deploy-state/current` and
`/opt/miloco-lab/deploy-state/previous`. The service is exposed only on port
`1810`. `miloco-staging-a.example.com` uses 3.0 CPUs and 3072m; `miloco-staging-b.example.com` uses 1.25
CPUs and 1536m. `compose.yaml` declares the CPU, memory, and process limits
that release validation checks after rendering.

The deployment root and every control, incoming, release, artifact, acceptance,
state, and lock path must be non-symlinked, root-owned, and within
`/opt/miloco-lab`. `status` and `verify` validate their paths and proof before
their first Docker or Compose observation; they do not create release evidence.
Application logs are never read or emitted by the release controller. If a real
AI-lab endpoint, model service, camera, or other external endpoint is absent,
record it as `not_measured`; a fixture or successful build is not a substitute.

## Contract-test isolation

The dynamic contract checks require an OS sandbox. macOS uses `sandbox-exec`;
Linux CI must provide Bubblewrap. Both deny network access, keep the repository
read-only, and permit writes only in the pytest temporary directory. If neither
backend is available, the suite fails before starting `deploy.sh`.

## Validated laboratory release

This public copy preserves the evidence shape from a private laboratory
rollout, with private hostnames anonymized to the example targets above. The
release validated on both laboratory hosts is Git SHA
`644406e36c621dfad55939686d315a2d3ddc955c`, with archive SHA-256
`8a057845415fa2a6d820449a9dc4744d4a18fb717300930174f3dd05e0522663`.
This remains the deployed runtime identity. The later documentation evidence
commit `3982f7a9484701eb826c1232cf8ab60e9cdbc94a` and post-deployment local
smoke compatibility commit `8480c6c5956236b71fffc88df4f5dfe7cc6443b0`
were not built or deployed. Any still-later closeout documentation commit is
also not a runtime release.

Both hosts passed the receipt-bound acceptance image before activation. The
gate exercised H.264/H.265 RTSP perception, the shared-session RTSP live path,
Responses JSON and SSE normalization, and Responses no-key and synthetic
Bearer behavior. These are deterministic fixture results. No persistent real
RTSP source or real local VLM endpoint was supplied, so
`real_camera=not_measured` and `real_vlm=not_measured`.

Final accepted runtime evidence, with target names anonymized:

- `miloco-staging-a.example.com`: exact image SHA and healthy status, UID/GID `10001:10001`,
  state mode `10001:10001:700`, CPU limit `3`, memory limit `3072m`, restart
  count `0`, current-process OOM `false`, final free disk `9025812` KiB, HTTP
  dashboard and RTSP/Responses/watch UI checks passed without console errors.
- `miloco-staging-b.example.com`: exact image SHA and healthy status, UID/GID `10001:10001`,
  state mode `10001:10001:700`, CPU limit `1.25`, memory limit `1536m`, restart
  count `0`, current-process OOM `false`, final free disk `24126448` KiB, HTTP
  `200`, dashboard build label `g644406e36`, navigation and console checks
  passed. Ten bounded samples over five minutes remained healthy on the same
  SHA.

The lab01 rollback drill found no distinct capable `previous` SHA. It therefore
re-activated the current capable runtime SHA without manufacturing history;
version differentiation is `not_applicable`. Final `verify` and `status` were
healthy, and the release tree, artifact record, acceptance marker, archive
digest, and mode-`0444` local receipt remained intact.

The repository release checks passed with explicit inherited baselines:
`./scripts/local-ci.sh --tests` passed all six script gates while retaining its
three documented Darwin node-monitor/smaps exclusions; the deployment contract
reported 181 passed; CLI reported 646 passed; Web reported 383 passed and one
skipped, with typecheck and build passing; Ruff lint and `git diff --check`
passed. Read-only repository-wide checks still report 298 files needing Ruff
formatting and 1007 `ty` diagnostics. `task lint` was not run and these
unrelated baselines were not rewritten.

The branch and recursive release-artifact scans found no private-key marker,
cloud-token form, credential-bearing RTSP URI, long base64 image, raw runtime
payload artifact, forbidden environment/config/repository/cache/venv path, or
credential-like filename. Authorization literals were limited to synthetic
tests. Structural inspection of both deployed containers found no API-key or
RTSP-credential environment-variable name and did not expose values.

One anonymized staging target required a user-performed memory expansion before
its accepted deployment. Unrelated observability and database services were not
restarted, removed, or otherwise operated on during that rollout. No upstream
push was performed.
