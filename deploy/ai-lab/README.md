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
at their exact canonical path, and resolved beneath `/opt/miloco-lab` before
use. The classifier checks the raw root and every named ancestor before the
resolved leaf, including read-only status paths; resolving through a parent
symlink is never accepted. Atomic writes validate both their parent and final
record.

If the requested SHA already has an intact release, marker, and exact image-ID
pair, it is reused without any marker, tag, or build mutation whether it is
`current`, `previous`, or one of the retained historical pairs. Any uncertainty
about such a pair fails closed without mutation. A confirmed-invalid protected
proof also fails before mutation; only a confirmed-invalid unprotected SHA may
enter isolated candidate rebuild. Every such build uses
`miloco-lab-candidate:<sha>` and
`miloco-lab-acceptance-candidate:<sha>` until acceptance succeeds; canonical
tags are untouched during build and acceptance. EXIT, HUP, INT, and TERM cleanup
is armed before the stale unprotected marker or any candidate tag is mutated.
Failure or session loss invalidates the unaccepted marker and verifies absence
of all candidate and unprotected canonical tags without touching a protected
SHA.

Only after isolated acceptance succeeds are candidate images promoted to the
canonical tags and re-inspected. Successful acceptance creates an atomic marker
bound to the archive digest and those canonical runtime and acceptance image
IDs. Verify, activation, rollback, and retention re-inspect both image IDs and
require an exact marker match. The cleanup trap remains armed through promotion
and marker publication, so interruption cannot leave a newly tagged but
unaccepted rollback candidate.

After a candidate starts, exit and signal traps remain armed until the new
`current` state is committed. An interrupted or unhealthy transition must
restore and re-check `previous`, or remove the first candidate and verify its
absence when no previous release exists. Health uses one strict 120-second
deadline: every Compose, inspect, and HTTP probe recomputes its remaining
budget, and a command that consumes that budget is killed without added grace.

Retention classifies every pair as rollback-capable, definitively invalid, or a
probe error. A missing image confirmed by a successful Docker listing, a
contract/checksum-invalid release, or an explicit marker/image-ID mismatch is
definitively invalid. Contract verification distinguishes a successfully
computed checksum or metadata mismatch from uncertainty: missing tools,
unexpected exit codes or output, and filesystem, read, `find`, `stat`, `grep`,
or checksum I/O failures are probe errors. `release.json` is parsed as JSON by
`python3`, with duplicate keys rejected. Its exact builder schema is required:
schema `1`, the full Git SHA, a UTC `built_at` timestamp, platform
`linux/amd64`, and the four build artifacts (`miloco`, `cli`, `miot`, and
`models`) bound to their actual wheel or model-archive files. Missing, extra,
conflicting, mis-typed, or control/acceptance artifact identities are rejected.
`SHA256SUMS`, artifact receipts, acceptance markers, file enumeration, per-file
hash output, image listings, and image inspection results must have one exact,
non-duplicated structure. The regular-file set derived from the full release
walk must equal the independent file-only walk and, excluding the manifest
itself, the unique checksum set; `SHA256SUMS` must appear exactly once in both
filesystem walks. Partial, extra, empty, duplicated, or malformed successful
enumeration output is a probe error, while equal walks that prove an extra or
missing checksummed payload file are a contract mismatch. Docker daemon/query
failures and unexpected image output are probe errors as well. Probe errors
stop cleanup without deleting anything.
Invalid debris does not consume a historical slot.
The current release plus two rollback-capable historical pairs are kept, and
`previous` consumes one historical slot.
Removal protects `current` and `previous`, removes and verifies both image tags
first, then deletes the exact release and its artifact and acceptance records;
any incomplete removal is reported. If cleanup fails after `current` is
committed, deploy reports `activated_cleanup_failed` but preserves successful
activation instead of pretending it rolled back. Failure evidence reports only
bounded container presence, normalized health, and HTTP status codes.
Application logs are never read or emitted by the release controller.

Read-only `status` performs the same canonical-path and filesystem-proof checks
before its first Compose or Docker query. The lab root, release parents, current
release, deployment-state parents, current record, artifact receipt, acceptance
marker, and complete release proof must all be non-symlinked, root-owned, at
their exact modes and real paths. An invalid or uncertain path or proof fails
closed without mutation and without contacting Docker.

When a real AI-lab endpoint, model service, camera, or other external endpoint
is absent, record that validation as `not_measured`. Do not substitute a mock,
placeholder endpoint, or a successful build for real endpoint evidence.
