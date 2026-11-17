# Linked PR implementation

Including a finding in review or report is not implementation permission. After its source
`accept` has been acknowledged, obtain a separate explicit approval to implement that one
finding. Record the approving actor and their exact words by creating the link:

The trusted controller must hold exclusive operational access to the implementation
repository and both session directories while `link` or `land` runs. Do not create a
worktree, check out the generated branch, or update its ref concurrently. The implementation
repository requires Git 2.36 or newer so object and reference writes can be explicitly
hardened against power loss. Keep the source session, `implementations/` tree, and local Git
metadata on private local POSIX storage that gateway jobs and other untrusted or concurrent
principals cannot modify.

```bash
$S/scripts/implementationctl.py link "$R" "$PWD" \
  --seq <source-accept-seq> --beat <beat> \
  --actor '<approving identity>' --approval '<explicit implementation approval>'
```

`link` rechecks the live PR, binds the source session, accept action, accepted finding,
frozen target, actor, and approval, then creates
`$R/implementations/<link-id>/`. Call that child directory `$C`. It contains exactly the
one approved finding, branch audience, `gateway_attested` execution, and a generated
`underwrite/implementation-<id>` local branch seeded from the frozen head. The source
review or report delivery remains independent. Repeating the same authorization resumes
the same child, including after the source beat has taken a note or landed its review.
The accepted finding itself cannot change while the authorization stands: `put-beat`
refuses it, and different prose is a different finding, needing its own accept, its own
acknowledgement and its own approval. `link` records the actor label but does not
authenticate it. Establish the approver's identity and authority through the trusted
controller before calling the command.

```bash
$S/scripts/implementationctl.py links "$R"
```

Every authorization this source session holds, with the actor and approval behind each one.
A `ready` link owns the child named by its `child_path`; a `reserved` one is a `link` that
died before it finished. Never point this command at a `child_path` a `reserved` link names:
that child does not exist yet, and the `link` that would create it is the only thing that may.

The host operator supplies a trusted profile `$P` outside the repository, session, and
gateway result. It contains exactly `version`, `keyId`, `signerId`, `executorId`, `job`,
`sandbox`, and `exitCode`. The operator also supplies an absolute pinned P-256 public key
path `$K`; never take either value from target code, returned evidence, or an envelope. The
profile is limited to 256 KiB, its canonical request to 384 KiB, `job.cwd` to 4,096 UTF-8
bytes with 255 bytes per component, the executable path to 4,095 bytes with the same
component limit, the complete Linux exec vector to 128 KiB, and `exitCode` to 0 through 255.
Reserve the request:

```bash
$S/scripts/implementationctl.py request "$C" "$R" "$P"
```

`request` rechecks the PR and binds a fresh challenge and attempt to the child action. A
retry returns the existing nonfailed attempt unchanged. Only a definitely failed attempt
gets a new number and challenge. It prints the request object as JSON. The trusted adapter
submits that object to the gateway; the later evidence file must be the gateway's exact
canonical `StoredExecution.request` bytes, not a copy of terminal formatting.

The one-shot adapter's handled stdout is a version 1 JSON object. `status: complete` with
exit code zero names the six-file evidence directory. `status: retry` with exit code one,
missing or malformed output, interruption, and every unknown outcome require retrying the
same request and source bundle. Only `status: failed` with exit code two proves the gateway
replay identity can no longer produce a receipt. Record only that outcome before
requesting a fresh attempt:

```bash
$S/scripts/implementationctl.py fail "$C" "$R" \
  --attempt <attempt> --reason '<definite gateway failure>'
```

Do not use `fail` for a bad evidence path, wrong local key, malformed transport, or failed
consumer verification. Those errors leave the same reserved attempt available for a
corrected, byte-identical handoff.

The supervised host adapter must return a real directory `$E` containing exactly these six
entries and no others:

```
request.json
capability.dsse.json
receipt.dsse.json
output.bundle
stdout
stderr
```

Each entry is an independently readable, single-link regular file. Consume it with the
same trusted profile and the pinned public key:

```bash
$S/scripts/implementationctl.py consume "$C" "$R" "$P" "$K" "$E"
```

`consume` requires the request bytes to equal the reserved request, verifies the frozen
source bundle and returned output bundle in fresh quarantines, hashes both complete
streams, verifies both DSSE signatures under `$K`, requires its fingerprint to match
`keyId`, and checks the exact signer, executor, target, action, job, sandbox, trees, bundle,
streams, and exit code. It then copies the exact evidence into
`$C/implementation-evidence/<attempt>/` and records its digests in the child database.
Neither the gateway's own validation record nor a conforming envelope substitutes for
these checks.
After `consume` durably succeeds, tell the gateway-owner supervisor it may delete the
handoff directory. The consumer account cannot delete it through the non-writable outbox.

Land only an attempt reported as verified:

```bash
$S/scripts/implementationctl.py land "$C" "$R" "$PWD" \
  --attempt <attempt> --message '<commit message>'
```

`land` materializes the verified output, creates a local SHA-1 commit whose only parent is
the frozen PR head, and remeasures that commit against the signed synthetic SHA-256 output
tree. It persists the exact commit plan before changing a ref, refuses a branch checked out
in any worktree, rechecks the live PR, and compare-and-swap updates the generated branch
only when it still names the frozen head. Git object, pack-metadata, and reference writes
use explicit `fsync` durability. Landing permits at most 20,000 output entries and has one
180-second deadline across verification, materialization, Git object creation, measurement,
live PR checks, and the ref update. It does not check out the branch, alter the working tree,
run hooks or filters, or push.

All five commands are recoverable. An identical `consume` pins the same profile and public
key, then compares all six incoming files with the persisted verified evidence. That exact
replay remains valid after the original capability window closes. An identical `land`
resumes its persisted commit plan and can finish the SQLite receipt if the ref already
names that commit. A reserved attempt rejects a changed profile, verified evidence is
immutable, and a prepared landing rejects a changed message, commit, branch, or ref
position. Exit 2 from `link`, `request`, or the pre-update `land` check means the PR moved
and requires a replacement source snapshot. If the PR moves only after the local
compare-and-swap, `land` preserves the commit and receipt and returns
`replacement_required: true`. Report that stale landing and stop. Never refresh either
session in place or cause another effect.
