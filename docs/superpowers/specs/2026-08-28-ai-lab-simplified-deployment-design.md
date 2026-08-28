# Miloco ai-lab Simplified Immutable Deployment Design

**Status:** Pending written-spec review

**Approved direction:** Disable automatic historical retention deletion and
same-SHA in-place rebuilds. Continue toward sequential lab deployment only
after this design and its implementation review are approved.

## 1. Context

Miloco's RTSP and OpenAI Responses feature work is complete locally. The
ai-lab deployment controller already provides content-addressed artifacts,
digest-addressed remote controllers, a single locked remote transaction,
isolated acceptance images, automatic activation rollback, and read-only
status/verification.

The previous design also tried to classify and delete old release directories,
images, artifact receipts, and acceptance markers automatically. Repeated
review showed that a destructive retention decision needs a perfect distinction
between confirmed corruption and transient filesystem or tool uncertainty.
That distinction added disproportionate state-machine complexity and remained
unsafe under uncommon partial-query and I/O-failure cases.

For two bounded laboratory hosts, automatic deletion is not required to prove
RTSP or Responses behavior. The deployment design will therefore preserve
historical evidence and fail closed when disk or release state is uncertain.

## 2. Goals

- Deploy an exact clean Git SHA sequentially to `ai-lab01.esxi`, then the same
  SHA to `ai-lab02.esxi`.
- Preserve content-addressed archive, controller, allowlist, artifact receipt,
  acceptance marker, and image-ID proof.
- Run fixture acceptance before service activation.
- Automatically restore the previously recorded healthy release when a new
  activation fails.
- Make every existing release SHA immutable: reuse it only when its complete
  proof is capable; otherwise reject it without mutation.
- Keep `status`, `verify`, and `rollback` read-only with respect to release
  evidence.
- Never delete a published historical release, canonical image pair, artifact
  receipt, or acceptance marker during build, deploy, verify, status,
  activation, rollback, or failure recovery.
- Stop before transfer when the existing preflight cannot prove at least
  5 GiB free disk.

## 3. Non-goals

- No automatic history-retention limit.
- No same-SHA repair, rebuild, overwrite, republish, or canonical-tag
  replacement.
- No cleanup command in the current six-command deployment interface.
- No background garbage collection.
- No deletion of current, previous, or unreferenced published releases.
- No production deployment, CO/PAM work, or upstream Xiaomi push.
- No claim that fixture acceptance proves a real RTSP camera or real local VLM
  endpoint; absent endpoints remain `not_measured`.

Historical cleanup is a separate future maintenance design with its own
authorization, tests, and review. It is not an implicit part of this rollout.

## 4. Selected Approach and Alternatives

### Selected: immutable accumulation

Every successfully published SHA remains on the host. A new SHA may be built,
accepted, activated, and automatically rolled back. An existing SHA may only
be reused after complete proof. Uncertain or invalid existing evidence blocks
the transaction without deleting or repairing anything.

This is the recommended approach because it removes destructive decisions from
the normal deployment path while preserving reproducibility and rollback
evidence.

Cost: disk use grows with each distinct SHA. The existing 5 GiB free-space
preflight is the hard stop. When it fails, deployment stops; it does not reclaim
space automatically.

### Rejected: continue strengthening automatic retention classification

This preserves bounded disk usage but keeps destructive behavior dependent on
perfect classification of partial command output, filesystem I/O errors, and
malformed state. Repeated review showed that this complexity is not justified
for the laboratory goal.

### Rejected: abandon lab deployment

This eliminates rollout risk but does not produce the requested Linux runtime,
RTSP fixture, Responses fixture, UI, health, resource-limit, or rollback
evidence.

## 5. Release Identity and Immutability

The full 40-character clean Git SHA remains the sole release identity. The
local build receipt continues to bind:

- schema;
- full Git SHA;
- archive SHA-256;
- clean remote-controller SHA-256;
- committed artifact-allowlist SHA-256;
- exact repository-relative archive path.

The remote artifact record and acceptance marker continue to bind the archive
digest and accepted runtime/acceptance image IDs.

For an existing SHA, the controller first completes the same incoming receipt,
archive, controller, allowlist, release-tree, record, marker, and image-ID
proof used for normal verification.

- `capable`: reuse the existing images and marker without building, retagging,
  rewriting, or deleting anything.
- `definitively_invalid`: fail the transaction; do not rebuild, overwrite,
  delete, or repair the SHA.
- `probe_error`: fail the transaction; do not rebuild, overwrite, delete, or
  repair the SHA.

No state pointer is required for immutability. Current, previous, and any older
published SHA follow the same rule.

## 6. New-SHA Transaction

For a SHA that has no published release directory, artifact record, acceptance
marker, or canonical image tags:

1. Validate the allowed host and exact clean local SHA.
2. Validate the build receipt before SSH.
3. Install and invoke the digest-addressed controller.
4. Acquire the host-wide transition lock.
5. Receive and validate the archive before extraction.
6. Atomically publish the verified release directory and artifact record.
7. Build isolated runtime and acceptance candidate tags.
8. Run the exact bounded fixture acceptance suite.
9. Promote the accepted image IDs to the canonical SHA tags.
10. Atomically publish the image-ID-bound acceptance marker.
11. Start the candidate release and run the strict 120-second health gate.
12. Atomically update `previous` and `current` only according to the existing
    activation state machine.
13. Return success without scanning or deleting any historical release.

The transaction must not invoke historical retention or pair removal after
activation.

## 7. Permitted Cleanup

Only unpublished transaction-local material may be removed automatically:

- local build staging directories and temporary archive/receipt names;
- remote incoming temporary archives and extraction staging directories;
- isolated candidate image tags that never acquired a durable acceptance
  marker;
- a failed candidate container;
- temporary atomic-write files that were never published.

Activation failure may restore the previous capable release and remove the
failed candidate container. It must not delete the newly published release
directory, canonical accepted image pair, artifact record, or acceptance
marker; those remain immutable evidence and can be inspected or reused.

Cleanup must never address another SHA and must preserve the current and
previous releases throughout compensation.

## 8. Existing-SHA Decision

An existing SHA is never a build target. Before any candidate cleanup, marker
change, Docker build, canonical retag, or state-pointer update:

- Complete proof `capable`: reuse, then continue to activation/health if the
  requested operation is `deploy` or `rollback`.
- Complete proof not capable: exit nonzero with fixed, non-secret metadata.
- Proof uncertainty: exit nonzero with fixed, non-secret metadata.

The operator must create a new clean Git commit to produce a new release
identity. A documentation-only commit is still a different SHA and must be
built and deployed consistently to both hosts if selected as the release.

## 9. Status, Verify, and Rollback

`status` remains read-only. Before Docker or Compose observation it validates
the raw lab root, deploy-state, current record, releases parent, exact release,
artifact-record parent and receipt, accepted parent and marker, ownership,
modes, non-symlink paths, exact realpaths, archive binding, release files, and
image-ID proof.

`verify` requires the current SHA to be capable, then runs the bounded health
gate. It never mutates release evidence.

`rollback HOST SHA` requires the exact target SHA to be capable. It uses the
same activation and health gate and restores the former current release if
activation fails. It does not delete either release.

## 10. Disk and Capacity Behavior

Preflight continues to require at least 5 GiB free disk before a transfer or
build. Failure is a hard, non-destructive stop. The controller must not invoke
retention or cleanup published releases to satisfy the threshold.

The final lab evidence records free disk before and after each host deployment.
It does not predict how many future releases fit. If capacity becomes
insufficient, the current rollout remains intact and a separate cleanup design
is required.

## 11. Security and Failure Evidence

- No application logs are collected by the deployment controller.
- Failure output remains fixed, structured metadata and normalized status.
- No RTSP URI, username, password, API key, token, authorization header,
  request body, response body, or base64 image enters artifacts, command
  output, progress documents, or commits.
- Build subprocesses continue to clear generic Omni, Responses, and RTSP
  credential variables.
- Existing owner, mode, realpath, symlink, archive-member, allowlist, checksum,
  record, and controller self-hash checks remain fail closed.

## 12. Test and Review Contract

The simplified controller must have executable regression tests proving:

- successful activation never calls historical retention or pair removal;
- no published release directory, canonical image pair, artifact record, or
  acceptance marker is removed after activation;
- any existing capable SHA is reused with zero build/tag/marker mutation;
- any existing invalid or uncertain SHA fails with zero mutation;
- no same-SHA rebuild occurs even when the SHA is not current or previous;
- transient candidate cleanup still removes only unpublished candidate state;
- failed activation restores the former current release without deleting
  either published proof;
- low disk preflight fails without a cleanup attempt;
- status/verify/rollback remain read-only for release evidence;
- the public deployment operations remain exactly `build`, `preflight`,
  `deploy`, `verify`, `status`, and `rollback`.

Required local gates before host access:

- complete deployment contract suite;
- scoped Ruff check;
- shell syntax checks;
- `git diff --check`;
- independent task review with no open Critical or Important findings.

Only after this design's implementation is approved may Task 4 package the
exact RTSP and Responses acceptance suite and proceed toward lab01, then lab02.

## 13. Acceptance Criteria

The simplified deployment task is complete when:

- automatic historical retention and pair deletion are absent from every
  deployment, activation, rollback, verify, and status path;
- existing SHAs are strictly reuse-or-fail and never rebuild targets;
- candidate-only cleanup remains bounded and compensation remains functional;
- the 5 GiB preflight remains non-destructive;
- executable deployment contracts pass;
- independent review reports no open Critical or Important finding;
- no real host access occurs before that approval.

