# Miloco AI-lab release contract

This directory defines the release payload and operator contract for the
isolated AI-lab deployment. The only permitted targets are `ai-lab01.esxi` and
`ai-lab02.esxi`; deployments must not be directed at another host.

`artifact-files.txt` is the sole release-payload allowlist. A release is built
from the current clean Git `HEAD`, with its exact SHA recorded in
`release.json`. It carries no repository metadata, local environments, runtime
configuration, or credentials.

## Operator commands

Run these six commands from the repository root after `deploy.sh` is supplied:

```bash
./deploy/ai-lab/deploy.sh build
./deploy/ai-lab/deploy.sh preflight --host ai-lab01.esxi
./deploy/ai-lab/deploy.sh deploy --host ai-lab01.esxi
./deploy/ai-lab/deploy.sh verify --host ai-lab01.esxi
./deploy/ai-lab/deploy.sh status --host ai-lab01.esxi
./deploy/ai-lab/deploy.sh rollback --host ai-lab01.esxi
```

Use the same command shape with `--host ai-lab02.esxi` for the second
permitted host. `build` refuses a dirty worktree. `preflight` validates the
selected host and release before any remote change. `deploy` automatically
rolls back to the last known-good release when its post-deploy verification
fails; an operator can also run the explicit `rollback` command.

`./deploy/ai-lab/deploy.sh --help` provides one machine-readable authoritative
line: `Operations: build preflight deploy verify status rollback`. Its prose
may describe those operations in any layout, but it must not declare a second
operations list or advertise a concrete operation in `Usage:`.

## Runtime boundary

Remote release state, including the active and last-known-good revisions, is
kept in `/var/lib/miloco-ai-lab`. The service is exposed on port `1810` only.
`compose.yaml` is required to declare CPU, memory, and process-count resource
limits; the release validation checks that those limits survive the rendered
Compose configuration.

When a real AI-lab endpoint, model service, camera, or other external endpoint
is absent, record that validation as `not_measured`. Do not substitute a mock,
placeholder endpoint, or a successful build for real endpoint evidence.
