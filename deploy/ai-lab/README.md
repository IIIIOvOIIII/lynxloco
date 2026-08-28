# Miloco AI-lab release contract

This directory defines the release payload and operator contract for the
isolated AI-lab deployment. The only permitted targets are `ai-lab01.esxi` and
`ai-lab02.esxi`; deployments must not be directed at another host.

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
MILOCO_SSH_IDENTITY=/absolute/path/to/lab-identity ./deploy.sh preflight ai-lab01.esxi
MILOCO_SSH_IDENTITY=/absolute/path/to/lab-identity ./deploy.sh deploy ai-lab01.esxi
MILOCO_SSH_IDENTITY=/absolute/path/to/lab-identity ./deploy.sh verify ai-lab01.esxi
MILOCO_SSH_IDENTITY=/absolute/path/to/lab-identity ./deploy.sh status ai-lab01.esxi
MILOCO_SSH_IDENTITY=/absolute/path/to/lab-identity ./deploy.sh rollback ai-lab01.esxi <full-40-character-sha>
```

Use the same positional command shape with `ai-lab02.esxi`, or the equivalent
`--host ai-lab02.esxi` form. `build` refuses a dirty worktree and does not need
an SSH identity. Every remote command requires `MILOCO_SSH_IDENTITY`: an
absolute regular file, not a symlink, owned by the current user, with no group
or other permission bits. The controller passes that path using SSH
`IdentitiesOnly=yes`; it never reads or prints its contents.

Every remote operation also requires both controllers to be tracked and the
whole worktree to be the exact clean `HEAD`. `preflight` validates the selected
host and release before any remote change. `deploy` restores the last
known-good release when post-deploy verification fails; an operator can also
run `rollback`. A failed restoration is reported as `rollback_failed` with
exit code `70`.

The two-host rollout is one build and two deployments: run `./deploy.sh build`
once, preserve its exact SHA-addressed archive and receipt unchanged, then
deploy that same full 40-character SHA first to `ai-lab01.esxi` and then to
`ai-lab02.esxi`. Do not rebuild, replace the archive, or create another SHA
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
`1810`. `ai-lab01.esxi` uses 3.0 CPUs and 3072m; `ai-lab02.esxi` uses 1.25
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
