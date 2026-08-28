# Miloco AI-lab release contract

This directory defines the release payload and operator contract for the
isolated AI-lab deployment. The only permitted targets are `ai-lab01.esxi` and
`ai-lab02.esxi`; deployments must not be directed at another host.

`artifact-files.txt` is the sole release-payload allowlist. A release is built
from the current clean Git `HEAD`, with its exact SHA recorded in
`release.json`. It carries no repository metadata, local environments, runtime
configuration, or credentials.

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
dirty worktree. `preflight` validates the
selected host and release before any remote change. `deploy` automatically
rolls back to the last known-good release when its post-deploy verification
fails; an operator can also run the explicit `rollback` command.

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

When a real AI-lab endpoint, model service, camera, or other external endpoint
is absent, record that validation as `not_measured`. Do not substitute a mock,
placeholder endpoint, or a successful build for real endpoint evidence.
