# Miloco AI-lab release contract

This directory defines the release payload and operator contract for the
isolated AI-lab deployment. The only permitted targets are `ai-lab01.esxi` and
`ai-lab02.esxi`; deployments must not be directed at another host.

`artifact-files.txt` is the sole release-payload allowlist. A release is built
from the current clean Git `HEAD`, with its exact SHA recorded in
`release.json`. It carries no repository metadata, local environments, runtime
configuration, or credentials. The atomic archive has an atomic mode-0444
receipt beside it. The receipt records schema `1`, the full Git SHA, archive
SHA-256, remote-controller SHA-256, allowlist SHA-256, and exact repository-
relative artifact path. `deploy` verifies every receipt field and recomputes
all three digests before opening SSH; an arbitrary ignored archive cannot be
endorsed at deploy time.

## Operator commands

Run the canonical controller from the repository root:

```bash
./deploy.sh build
./deploy.sh preflight ai-lab01.esxi
./deploy.sh deploy ai-lab01.esxi
./deploy.sh verify ai-lab01.esxi
./deploy.sh status ai-lab01.esxi
./deploy.sh rollback ai-lab01.esxi <full-40-character-sha>
```

Use the same positional command shape with `ai-lab02.esxi` (or the equivalent
`--host ai-lab02.esxi` form) for the second permitted host. `build` refuses a
dirty worktree. Every remote operation also refuses to start SSH unless both
controllers are tracked and the whole worktree is the exact clean `HEAD`.
`preflight` validates the selected host and release before any remote change.
`deploy` automatically rolls back to the last known-good release when its
post-deploy verification fails; an operator can also run the explicit
`rollback` command. A failed recovery is reported distinctly as
`rollback_failed` with exit code `70`.

`./deploy.sh --help` provides one machine-readable authoritative
line: `Operations: build preflight deploy verify status rollback`. Its prose
may describe those operations in any layout, but it must not declare a second
operations list or advertise a concrete operation in `Usage:`.

## Contract-test isolation

Once `deploy.sh` exists, the dynamic contract checks require an OS sandbox:
macOS uses the built-in `sandbox-exec`, while Linux CI must install Bubblewrap
and expose its `bwrap` executable. Both paths deny network access, make the
repository read-only, and permit writes only in the pytest temporary directory.
If neither backend is available, the suite fails before starting `deploy.sh`;
it never skips or runs the deployment CLI without isolation.

## Runtime boundary

Persistent application state is kept at `/opt/miloco-lab/state`. Deployment
state is kept separately in `/opt/miloco-lab/deploy-state/current` and
`/opt/miloco-lab/deploy-state/previous`; status never creates either path.
Immutable releases are kept under `/opt/miloco-lab/releases/<sha>`. The service
is exposed on port `1810` only. `ai-lab01.esxi` uses 3.0 CPUs and 3072m;
`ai-lab02.esxi` uses 1.25 CPUs and 1536m.
`compose.yaml` is required to declare CPU, memory, and process-count resource
limits; the release validation checks that those limits survive the rendered
Compose configuration.

## Transfer and rollback safety

The local controller calculates an independent SHA-256 digest for the release
archive and sends the receipt-bound digest as a validated argument. Before the
transaction it installs the clean controller create-if-absent at
`/opt/miloco-lab/control/<controller-sha256>/remote-release.sh`, verifies the
existing-or-new file, and uses that exact immutable path for both preflight and
transaction. The controller re-hashes its own exact path while holding the
transition lock, closing the shared-controller replacement race.

One deploy transaction and one host-wide lock cover receive, archive and release
verification, both image builds, acceptance-marker invalidation, acceptance,
activation, and retention. The remote controller first receives the archive
into a root-owned private temporary file. It checks the independent digest and
rejects unsafe, duplicate, linked, special, escaping, or non-allowlisted archive
members before extracting anything. Extraction occurs only in private staging,
ignores archive ownership and modes, normalizes every member to root ownership
and safe modes, verifies `SHA256SUMS`, and then publishes the exact release
directory atomically. An identical verified artifact can be received again
after a failed acceptance run without extracting over the existing release.

The deployment root and every control, incoming, release, artifact, acceptance,
state, and lock parent or record are required to be non-symlinks, root-owned,
and resolved beneath `/opt/miloco-lab` before use. Atomic writes validate both
their parent and final record.

Before any same-SHA image build, an earlier acceptance marker is removed and old
tags are removed. Successful acceptance creates an atomic marker bound to the
archive digest and the inspected runtime and acceptance image IDs. Verify,
activation, rollback, and retention re-inspect both image IDs and require an
exact marker match. A failed build or acceptance removes and verifies absence of
both tags, so an old marker plus a rebuilt tag cannot become rollback-capable.

After a candidate starts, exit and signal traps remain armed until the new
`current` state is committed. An interrupted or unhealthy transition must
restore and re-check `previous`, or remove the first candidate and verify its
absence when no previous release exists. Health uses one strict 120-second
deadline: every Compose, inspect, and HTTP probe recomputes its remaining
budget, and a command that consumes that budget is killed without added grace.

Retention classifies every pair as rollback-capable, definitively invalid, or a
probe error. Probe errors stop cleanup without deleting anything. Invalid debris
does not consume a historical slot. The current release plus two rollback-
capable historical pairs are kept, and `previous` consumes one historical slot.
Removal protects `current` and `previous`, removes and verifies both image tags
first, then deletes the exact release and its artifact and acceptance records;
any incomplete removal is reported. If cleanup fails after `current` is
committed, deploy reports `activated_cleanup_failed` but preserves successful
activation instead of pretending it rolled back. Failure evidence reports only
bounded container presence, normalized health, and HTTP status codes.
Application logs are never read or emitted by the release controller.

When a real AI-lab endpoint, model service, camera, or other external endpoint
is absent, record that validation as `not_measured`. Do not substitute a mock,
placeholder endpoint, or a successful build for real endpoint evidence.
