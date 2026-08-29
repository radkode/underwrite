# Underwrite host execution protocol

Status: version 1

This document defines the signed artifacts that a trusted execution gateway must issue
before and after running an Underwrite job against an untrusted pull request. It defines
wire formats and validation. It does not enable execution in Underwrite by itself.

The key words MUST, MUST NOT, REQUIRED, SHALL, SHALL NOT, SHOULD, SHOULD NOT, RECOMMENDED,
MAY, and OPTIONAL are to be interpreted as described by RFC 2119 and RFC 8174 when they
appear in uppercase.

## Security model

Underwrite trusts a configured execution gateway to enforce a sandbox while it runs one
exact job against one exact frozen target. The gateway proves its claims with two signed
artifacts:

1. A host capability binds a short-lived, single-use invocation to a verified input tree
   and the sandbox controls that will govern it.
2. An execution receipt binds the same invocation to the resulting tree, output bundle,
   process result, and complete captured streams.

Both artifacts are DSSE envelopes containing in-toto Statements. DSSE authenticates the
payload bytes and payload type. It does not define signer identity, key management,
freshness, authorization, or the truth of the sandbox claims. Those properties come from
the verifier's out-of-band trust configuration and the gateway implementation.

The signing key and signing operation MUST remain outside the sandbox trust boundary.
Target-controlled processes MUST NOT read key material, call a general signing service, or
choose attestation payload bytes. The trusted gateway constructs and signs only the
capability it verified before launch and the receipt it measured after teardown. Verifier
keys and allowed signer, executor, and policy tuples likewise MUST come from configuration
outside the repository, session, job, and returned artifacts.

The fixed identifiers for version 1 are:

| Purpose | Value |
| --- | --- |
| DSSE payload type | `application/vnd.in-toto+json` |
| in-toto Statement type | `https://in-toto.io/Statement/v1` |
| Host capability predicate | `https://github.com/radkode/underwrite/attestations/host-capability/v1` |
| Execution receipt predicate | `https://github.com/radkode/underwrite/attestations/execution-receipt/v1` |
| Host capability storage media type | `application/vnd.in-toto.host-capability+dsse` |
| Execution receipt storage media type | `application/vnd.in-toto.execution-receipt+dsse` |

## Encoding profile

### Canonical JSON

Objects whose digest is named by this protocol use the following canonical serialization:

```python
json.dumps(
    value,
    ensure_ascii=False,
    separators=(",", ":"),
    sort_keys=True,
).encode("utf-8")
```

In version 1 this canonical JSON rule applies only to `target.document` and `job`. It does
not apply to the Statement payload bytes.

The value MUST first pass its schema validation. Floats are not allowed anywhere in the
version 1 objects. An implementation in another language MUST produce the same UTF-8
bytes. Object keys are sorted by Unicode code point, non-ASCII characters are emitted as
UTF-8, and there is no insignificant whitespace or trailing newline.

Every JSON parser used by the validator MUST reject duplicate object keys. Unless a field
is explicitly described as optional, every Statement, predicate, invocation, and nested
protocol object MUST contain exactly the listed fields. Unknown fields are rejected. This
strictness is part of the two Underwrite predicate definitions.

Hexadecimal digests MUST use lowercase ASCII. `sha256` values contain exactly 64
characters. `gitTree` values in this protocol also contain exactly 64 characters because
version 1 always uses the SHA-256 Git object format.

Byte counts are non-negative JSON integers and MUST NOT be JSON booleans. Limit values,
action coordinates, and pull request numbers are positive JSON integers. Every protocol
integer is at most 9,007,199,254,740,991, JSON's interoperable safe integer maximum.

Timestamps are RFC 3339 strings in UTC and MUST end in `Z`. They contain whole seconds and
MAY contain one through six fractional-second digits. Comparisons use their parsed
instants, not lexical ordering.

### DSSE envelope

Each artifact is represented by exactly one DSSE signature:

```text
{
  "payloadType": "application/vnd.in-toto+json",
  "payload": "<base64 of the exact Statement bytes>",
  "signatures": [
    {
      "keyid": "<key lookup hint>",
      "sig": "<base64 signature>"
    }
  ]
}
```

The envelope MUST contain `payloadType`, `payload`, and `signatures`. Its signatures array
MUST contain exactly one signature object with nonempty `keyid` and `sig`. Envelope and
signature extension fields are ignored. Producers MAY use canonical padded RFC 4648
standard or URL-safe base64. Verifiers MUST accept both canonical padded forms, but MUST
reject malformed encodings and mixed alphabets.

The reference validator limits an envelope to 1,000,000 bytes, its decoded payload to
512,000 bytes, and its decoded signature to 16,384 bytes.

The signature input is the DSSE pre-authentication encoding of the decoded payload bytes:

```text
PAE(type, body) =
  "DSSEv1" + SP + LEN(type) + SP + type + SP + LEN(body) + SP + body
```

`type` is the UTF-8 encoding of `payloadType`. `body` is the decoded `payload`. `SP` is one
ASCII space byte. `LEN` is the ASCII decimal byte length with no leading zeroes.

The validator MUST construct PAE from the received payload type and decoded payload, call
its configured signature verifier, and accept the returned signer identity before parsing
the payload as JSON. The exact verified payload bytes MUST be retained and passed to every
later validation step. A parser MUST NOT decode the envelope a second time to obtain a
different payload.

`keyid` is an unauthenticated lookup hint. It MAY narrow the keys tried by the signature
verifier, but MUST NOT establish signer identity, select an authorization policy, or make
any other security decision. The DSSE envelope does not carry a trusted signature
algorithm. The signature verifier supplies both algorithm policy and trusted keys.

### in-toto Statement

Every payload is a UTF-8 JSON object containing exactly `_type`, `subject`, `predicateType`,
and `predicate`. Its raw bytes need not use the canonical encoding because DSSE signs
those exact bytes. `subject` contains exactly one ResourceDescriptor. That descriptor
contains exactly `name` and `digest`. The predicate-specific sections below define their
values.

The base in-toto specification permits an omitted predicate. Underwrite's two predicates
do not. Their `predicate` objects are REQUIRED and exact.

## Invocation

The capability and receipt contain the same invocation object:

```json
{
  "session": {
    "id": "<Underwrite session identifier>",
    "challenge": "<64 lowercase hex characters>"
  },
  "target": {
    "document": {
      "version": 1,
      "kind": "github_pr",
      "repo": "owner/name",
      "number": 42,
      "state": "open",
      "merged_at": null,
      "base_sha": "<40 lowercase hex characters>",
      "head_sha": "<40 lowercase hex characters>",
      "head_repo_id": 1234,
      "head_repo": "fork-owner/name",
      "head_ref": "feature-branch",
      "merge_base_sha": "<40 lowercase hex characters>",
      "changed_files": 7,
      "diff_sha256": "<64 lowercase hex characters>",
      "diff_bytes": 2048,
      "trusted_context_sha256": "<64 lowercase hex characters>",
      "trusted_context_bytes": 1024,
      "object_bundle_sha256": "<64 lowercase hex characters>",
      "object_bundle_bytes": 4096
    },
    "digest": {
      "sha256": "<SHA-256 of the canonical target document>"
    }
  },
  "sourceBundle": {
    "sha256": "<64 lowercase hex characters>",
    "bytes": 4096
  },
  "action": {
    "seq": 12,
    "beat": 3,
    "attempt": 1
  },
  "job": {
    "argv": ["/usr/bin/python3", "-m", "unittest"],
    "cwd": ".",
    "environment": {
      "LANG": "C.UTF-8"
    },
    "executable": {
      "path": "/usr/bin/python3",
      "sha256": "<64 lowercase hex characters>",
      "bytes": 6839896
    },
    "stdin": "closed"
  },
  "jobDigest": {
    "sha256": "<SHA-256 of the canonical job object>"
  },
  "inputTree": {
    "gitTree": "<64 lowercase hex characters>"
  },
  "sandbox": {
    "policy": "https://github.com/radkode/underwrite/sandbox-policy/v1",
    "credentials": "absent",
    "network": "denied",
    "hostWrites": "denied",
    "gitHooks": "disabled",
    "gitFilters": "disabled",
    "timeout": "enforced",
    "limits": {
      "wallSeconds": 900,
      "cpuSeconds": 600,
      "memoryBytes": 4294967296,
      "processes": 256,
      "workspaceBytes": 1073741824,
      "outputBytes": 16777216
    }
  }
}
```

### Session and replay identity

`session.id` is the stable canonical lowercase UUID of the frozen Underwrite session.
`session.challenge` is 32 cryptographically random bytes generated by Underwrite for this
attempt and encoded as 64 lowercase hexadecimal characters. A challenge MUST NOT be
reused.

The replay identity is the tuple of signer identity, executor ID, session ID, challenge,
action sequence, beat, attempt, and job digest. The gateway integration MUST reserve this
tuple before issuing a capability and permit at most one job and one terminal receipt for
it.

The version 1 envelope validator has no replay-store input and does not consume this tuple.
DD-2174 must add the integration that consumes the tuple and capability payload digest
transactionally before accepting a receipt. A byte-identical replay MAY return the already
recorded result. Any reuse with a different capability, invocation, receipt, or artifact
is a conflict and MUST be rejected.

### Frozen target

`target.document` is the full current Underwrite pull request target. It includes every
field shown above and no others. `merged_at` is either null or nonempty RFC 3339 text.
`head_repo` and `head_repo_id` are both null for an unavailable deleted fork, or both have
the types shown. `state` is `open` or `closed`. `changed_files` and `diff_bytes` are
non-negative integers. `trusted_context_bytes` and `object_bundle_bytes` are positive
integers.

The three commit fields are full GitHub SHA-1 object IDs and therefore exactly 40
lowercase hexadecimal characters. The three SHA-256 fields are exactly 64 lowercase
hexadecimal characters.

`target.digest.sha256` is SHA-256 of the canonical JSON bytes of `target.document`.
Underwrite MUST compare the received document structurally with its authoritative frozen
target, then recompute and compare the digest. It MUST NOT accept a digest as a substitute
for the full field comparison.

`sourceBundle.sha256` and `sourceBundle.bytes` identify the exact `pr.bundle` bytes given
to the gateway. They MUST equal `target.document.object_bundle_sha256` and
`target.document.object_bundle_bytes` respectively, so the byte count is positive.

### Action

`action.seq`, `action.beat`, and `action.attempt` are positive integers. They bind one
execution attempt to the authoritative queued action and finding beat. A retry uses a new
attempt and a new challenge unless it is a byte-identical replay of an already recorded
result.

### Job

`job.argv` is a nonempty array of UTF-8 strings without NUL characters. `argv[0]` is
nonempty; later arguments MAY be empty. It is an argument vector, not a shell command.
The gateway MUST call the configured executable directly and MUST NOT invoke a shell to
interpret any element. `argv[0]` MUST equal `job.executable.path`.

`job.cwd` is a normalized, repository-relative POSIX path. `.` names the source root.
Absolute paths, empty components, `.`, `..`, backslashes, NUL characters, and control
characters are forbidden within any other value. The resolved directory MUST remain
inside the isolated workspace.

`job.environment` is the complete effective environment for the child process, not a set
of overrides applied to an inherited host environment. Keys and values are UTF-8 strings
without NUL characters. Keys are nonempty and cannot contain `=`; values MAY be empty. The
gateway MUST start from an empty environment and install exactly these entries. Ambient
credentials and credential helper variables are forbidden.

`job.executable.path` is a normalized absolute path to the file the gateway will execute.
Its digest and positive byte count cover the exact regular-file bytes. The gateway MUST
verify and pin that file before capability issuance and execute the same immutable file. A
later path lookup is not sufficient. The executable MUST come from the trusted host image,
not the untrusted source tree.

`job.stdin` is the literal string `closed`. The child receives an already closed standard
input and cannot inherit a terminal or caller stream.

`jobDigest.sha256` is SHA-256 of the canonical JSON bytes of the complete `job` object.
Both gateway and validator recompute it.

### Sandbox

`sandbox.policy` is exactly
`https://github.com/radkode/underwrite/sandbox-policy/v1`. The trusted signer is authorized
only for an explicit set of executor and policy URI pairs.

All other sandbox fields have the fixed literal values shown in the schema. In version 1
they mean:

- `credentials: "absent"`: no ambient credentials, tokens, SSH agents, cloud metadata,
  credential helpers, or inherited secret-bearing environment are visible.
- `network: "denied"`: the job and all descendants have no network access, including
  loopback and Unix sockets that cross the sandbox boundary.
- `hostWrites: "denied"`: no job process can write outside sandbox-owned ephemeral
  storage and the declared output capture.
- `gitHooks: "disabled"`: no Git hook can execute during source ingestion, materialization,
  execution, or output packaging.
- `gitFilters: "disabled"`: no clean, smudge, process, textconv, diff, or external merge
  filter can execute or transform content.
- `timeout: "enforced"`: the gateway terminates the complete process tree at the declared
  wall or CPU limit.

Every limit is a positive integer and applies to the job and all descendants together.
`workspaceBytes` limits the writable isolated workspace. `outputBytes` limits stdout and
stderr together. Exceeding any limit prevents issuance of a conforming version 1 execution
receipt.

The gateway MUST own the isolated workspace exclusively. It measures `inputTree` while
the workspace is quiescent, then permits the job to modify that workspace. It stops every
descendant before measuring `outputTree`. No other process may write the workspace during
either measurement. Input and output trees MAY differ. The trusted gateway's signed
sandbox claim, not equality of those trees, is the root of the host-isolation guarantee.

## Synthetic SHA-256 Git tree

`inputTree.gitTree`, `outputTree.gitTree`, and `outputBundle.gitTree` use a synthetic Git
tree derived from execution-visible filesystem content. This is the in-toto `gitTree`
DigestSet algorithm using Git's SHA-256 object format. It is independent of the source
repository's SHA-1 object format.

The gateway MUST use a descriptor-relative traversal that does not follow symlinks. It
MUST include every entry below the workspace root. There are no ignored paths. Gateway
metadata such as `.git` MUST not be present in the workspace. The following mappings
are allowed:

- A regular file is a Git blob. Its tree mode is `100755` when any executable bit is set,
  otherwise `100644`. Its blob body is the exact file bytes.
- A symbolic link is a Git blob with tree mode `120000`. Its blob body is the exact link
  target byte string returned by `readlink`. The target is not followed.
- A nonempty directory is a Git tree with mode `40000`. Its body recursively identifies
  its children.

Empty directories, hard links, submodules, sockets, devices, FIFOs, and every other file
type are rejected. Names are the exact path-component bytes exposed by the filesystem.
NUL and `/` cannot occur within a component. Hosts that cannot preserve the source bundle's
path bytes exactly are unsupported by version 1.

For bytes `content`, a blob object ID is:

```text
SHA256("blob" + SP + DECIMAL(len(content)) + NUL + content)
```

A tree body is the concatenation of one entry for each direct child:

```text
ASCII(mode) + SP + name + NUL + raw_32_byte_child_object_id
```

Entries use Git's canonical tree ordering. Compare name bytes unsigned from left to right.
When one name ends, use `/` as its next comparison byte when that entry is a directory,
otherwise use NUL. A tree object ID is:

```text
SHA256("tree" + SP + DECIMAL(len(tree_body)) + NUL + tree_body)
```

The root tree object ID, encoded as 64 lowercase hexadecimal characters, is the `gitTree`
value. The gateway MUST reject a file that changes type, mode, size, or identity while it
is being measured. Exclusive, quiescent measurement SHOULD make such a race impossible,
but the tree walker must still fail closed.

## Host capability

The host capability Statement has this exact shape:

```text
{
  "_type": "https://in-toto.io/Statement/v1",
  "subject": [
    {
      "name": "underwrite-execution-input",
      "digest": {
        "gitTree": "<same value as invocation.inputTree.gitTree>"
      }
    }
  ],
  "predicateType": "https://github.com/radkode/underwrite/attestations/host-capability/v1",
  "predicate": {
    "executor": {
      "id": "<trusted executor ID>"
    },
    "issuedAt": "2026-08-29T12:00:00Z",
    "expiresAt": "2026-08-29T12:05:00Z",
    "invocation": INVOCATION
  }
}
```

`INVOCATION` above is replaced directly by the JSON object defined in the Invocation
section. It is not quoted and is not a reference object in an actual payload.

`executor.id` is exactly `executorId` from trusted expected context. The signature callback
must separately return `signerId` from that context. The executor ID identifies the
gateway implementation and security mode but is not self-authenticating without the
signature check. Out-of-band policy MUST allow the exact signer ID, executor ID, and
sandbox policy tuple.

`issuedAt` is the instant after the isolated workspace, executable, source bundle, target,
job, and sandbox have been verified and reserved, but before any target-controlled process
is started. `expiresAt` is later than `issuedAt` by no more than 300 seconds. The gateway
and validator both enforce the expiry against trusted local clocks. The job MUST start no
earlier than `issuedAt` and strictly before `expiresAt`.

The capability subject MUST equal `invocation.inputTree`. A capability is a commitment by
a configured gateway. It is not evidence that the job ran, that it succeeded, or that its
output is safe.

## Execution receipt

The execution receipt Statement has this exact shape:

```text
{
  "_type": "https://in-toto.io/Statement/v1",
  "subject": [
    {
      "name": "underwrite-execution-output",
      "digest": {
        "gitTree": "<same value as predicate.outputTree.gitTree>"
      }
    }
  ],
  "predicateType": "https://github.com/radkode/underwrite/attestations/execution-receipt/v1",
  "predicate": {
    "executor": {
      "id": "<same trusted executor ID as the capability>"
    },
    "startedAt": "2026-08-29T12:00:01Z",
    "finishedAt": "2026-08-29T12:00:11Z",
    "invocation": INVOCATION,
    "capability": {
      "payloadSha256": "<SHA-256 of decoded capability payload bytes>"
    },
    "outputTree": {
      "gitTree": "<64 lowercase hex characters>"
    },
    "outputBundle": {
      "gitTree": "<same value as outputTree.gitTree>",
      "sha256": "<64 lowercase hex characters>",
      "bytes": 8192
    },
    "result": {
      "status": "exited",
      "exitCode": 0,
      "signal": null,
      "timedOut": false,
      "resourceViolation": null,
      "isolationViolation": null,
      "survivingProcesses": 0,
      "teardown": "complete"
    },
    "streams": {
      "stdout": {
        "sha256": "<64 lowercase hex characters>",
        "bytes": 512,
        "truncated": false
      },
      "stderr": {
        "sha256": "<64 lowercase hex characters>",
        "bytes": 0,
        "truncated": false
      }
    }
  }
}
```

The two `invocation` values MUST be structurally equal and their canonical JSON bytes MUST
be identical. `capability.payloadSha256` is SHA-256 of the exact decoded capability
`payload` bytes after its signature has been verified. It is not a digest of parsed JSON,
the base64 text, or the DSSE envelope. This link remains stable if the outer envelope is
transported with different JSON whitespace.

`startedAt` is no earlier than the capability's `issuedAt` and strictly earlier than its
`expiresAt`. `finishedAt` is no earlier than `startedAt` and respects the wall limit. The
receipt validator rejects a `finishedAt` later than its trusted current time. The gateway
issues a version 1 receipt only after the child and every descendant have exited, the
streams have been captured completely, the output tree has been measured, and the output
bundle has been sealed.

`result` has exactly the fields shown above. `status` is the literal `exited`, `signal`,
`resourceViolation`, and `isolationViolation` are null, `timedOut` is false,
`survivingProcesses` is the integer zero, and `teardown` is the literal `complete`.
`exitCode` is the actual non-negative integer process exit code. The validator receives an
exact expected exit code from trusted caller context and requires equality. A caller may
intentionally expect a nonzero exit code. The receipt authenticates that outcome but never
authorizes it by itself.

A signal, timeout, execution failure, sandbox violation, resource-limit violation,
surviving process, incomplete teardown, incomplete stream, or artifact-verification
failure MUST NOT produce a conforming version 1 execution receipt. A gateway MAY preserve
separately signed diagnostic evidence, but it is not this predicate.

The stdout and stderr records cover the complete raw byte streams in production order.
Their byte counts and SHA-256 digests are computed independently. `truncated` is the JSON
boolean false and no other value is valid. The gateway returns the stream bytes alongside
the receipt so that Underwrite can verify them. If either stream is unavailable or
truncated, no valid receipt exists. The sum of `stdout.bytes` and `stderr.bytes` MUST NOT
exceed `invocation.sandbox.limits.outputBytes`.

`outputTree.gitTree` is measured directly from the isolated workspace after execution.
The Statement subject identifies that output tree and MUST equal `outputTree.gitTree`.
`outputBundle` identifies the returned Git bundle transport bytes and the synthetic
SHA-256 tree that the bundle exposes as its output. `outputBundle.gitTree` MUST equal both
`outputTree.gitTree` and the Statement subject. `outputBundle.bytes` is positive.

## Artifact verification integration

The version 1 envelope validator does not receive source bundle, output bundle, stdout, or
stderr bytes. It compares signed fields with caller-owned expected descriptors. Those
descriptors are trustworthy only when an independent integration derives them from the
actual bytes rather than copying them from an envelope or an untrusted job.

DD-2174 must add this integration. For the source bundle it MUST:

1. Check its exact byte count and SHA-256 against `sourceBundle`.
2. Check those values against the frozen target's `object_bundle_bytes` and
   `object_bundle_sha256`.
3. Import the bundle into a new bare quarantine repository with an empty template, hooks
   and filters disabled, no inherited repository configuration, no submodule recursion,
   and no network access.
4. Require `refs/underwrite/base` and `refs/underwrite/head` to resolve to the frozen
   `base_sha` and `head_sha`.
5. Materialize the head without running target-controlled code and require the resulting
   synthetic tree to equal `inputTree.gitTree`.

For the output bundle it MUST:

1. Check its exact byte count and SHA-256 against `outputBundle`.
2. Import it into a fresh quarantine repository under the same restrictions used for the
   source bundle.
3. Resolve its declared output ref without checking it out or executing Git extensions,
   then compute the synthetic SHA-256 tree represented by that ref.
4. Require that tree to equal `outputBundle.gitTree`, `outputTree.gitTree`, and the
   Statement subject.
5. Reject missing objects, unexpected prerequisites, malformed refs, replace-object
   behavior, alternates, partial-clone promises, submodules, and any content that requires
   an external fetch or filter.

The sandbox policy MUST define the single output ref and allowed bundle prerequisites.
The integration receives those values from trusted configuration. It MUST NOT infer them
from untrusted bundle names or choose among multiple candidate refs.

The integration MUST also hash the exact stdout and stderr bytes independently, derive
their `sha256` and `bytes` descriptors, and require both streams to be complete. It passes
only those independently derived descriptors to the envelope validator. Likewise, it
passes independently derived `inputTree`, `outputTree`, and output bundle descriptors.
The current validator authenticates equality with signed fields but does not itself open,
hash, import, or inspect any artifact.

After integration verification, Underwrite may inspect or apply the bundle only through
its existing trusted, argument-vector-safe Git gateways. Validation does not authorize an
automatic checkout, commit, push, review, or other external effect.

## Validator contract

The validator receives three classes of input:

- The capability envelope bytes and, when validating completion, the receipt envelope
  bytes.
- Trusted expected context from the caller. It contains separate `signerId` and
  `executorId`, the authoritative invocation inputs, independently derived tree, bundle,
  and stream descriptors, and the exact non-negative expected exit code. A timezone-aware
  current time is a separate trusted input.
- A signature callback that verifies one PAE byte string and raw signature under configured
  keys and returns a stable trusted signer identity.

Capability expected context contains exactly `signerId`, `executorId`, `sessionId`,
`challenge`, `target`, `action`, `job`, `inputTree`, and `sandbox`. Receipt validation adds
exactly `outputTree`, `outputBundle`, `stdout`, `stderr`, and `exitCode`.

Conceptually, the callback is:

```text
verify(pae: bytes, keyid: str, signature: bytes) -> signer_identity | reject
```

The validator, not the callback, checks the Statement, executor ID, predicate type,
sandbox policy, invocation, and expected descriptors. The callback treats `keyid` only as
a search hint and derives `signer_identity` from the key that actually verifies the
signature. Before calling the validator, the integration MUST establish that the exact
`signerId`, `executorId`, and sandbox policy tuple is allowed.

Validation proceeds in this order:

1. Decode each exact one-signature DSSE envelope and retain its raw decoded payload.
2. Construct PAE and verify the signature before parsing the payload.
3. Require the returned signer identity to equal expected `signerId`, and the signed
   executor ID to equal expected `executorId`. Capability and receipt MUST have the same
   trusted signer and executor IDs.
4. Require the fixed payload type, parse the exact UTF-8 JSON payload with duplicate-key
   rejection, and validate the exact in-toto Statement and predicate shape.
5. Compare the capability invocation with trusted expected context, recomputing the target
   and job digests. Check its subject, timestamps, and freshness.
6. Hash the verified capability payload and require the receipt link to match. Require the
   receipt invocation to be canonically identical to the capability invocation.
7. Check receipt timestamps, exact clean-exit result, caller-expected exit code, complete
   stream descriptors, and all sandbox limits.
8. Require the signed stream, output bundle, output tree, and output subject descriptors
   to equal the independently derived expected descriptors.

Every comparison is fail closed. A validator failure returns no execution authority, even
when some independent evidence inside the artifacts is valid.

DD-2174 is responsible for deriving the expected descriptors, verifying artifacts as
described above, reserving and consuming replay identity, calling this validator, and
persisting the capability, receipt, artifacts, signer identity, and validation result in
one authoritative transaction.

## Non-authorization and rollout

This protocol does not change the current `no_exec` execution policy. A caller-supplied
envelope, sandbox label, executor ID, receipt, or tree digest is untrusted until the entire
validator contract succeeds against authoritative expected context and a configured
signer.

A host capability never authorizes Underwrite to treat target code as executed. Only a
valid linked receipt can provide evidence of one completed job. Even then, the receipt is
input to a higher-level Underwrite policy. It does not itself authorize applying the
output bundle or causing an external effect.

Underwrite must retain `no_exec` until a trusted gateway, signer provisioning, validator,
artifact quarantine path, replay store, and supervised application path are implemented
and tested end to end. Protocol conformance alone is insufficient.

## Limitations

- Current Codex and Claude Code plugin, skill, and hook interfaces do not issue this
  per-invocation signed capability and receipt pair. An adapter that merely copies their
  sandbox labels or hook events cannot satisfy this protocol. A trusted execution gateway
  must own enforcement, measurement, signing, and artifact capture.
- The gateway signer is a root of trust. A valid signature proves what the trusted gateway
  claimed, not that an independent party observed the sandbox.
- The protocol does not provide hardware attestation, measured boot, transparency-log
  inclusion, key revocation, or compromise recovery.
- The executable digest does not cover the kernel, dynamic loader, shared libraries,
  interpreter, imported modules outside the measured workspace, or the complete host image.
  The executor ID and sandbox policy must bind an acceptably controlled runtime.
- The synthetic tree intentionally omits timestamps, ownership, ACLs, extended attributes,
  and non-executable permission bits. The sandbox policy must normalize or make those
  properties irrelevant.
- Empty directories and special files are unsupported. A target containing them cannot use
  version 1 execution.
- The input and output tree hashes attest only their two measured states. They do not
  provide a filesystem event trace or identify transient intermediate states.
- Receipt timestamps depend on trusted gateway and validator clocks. The 300-second
  capability lifetime bounds, but does not eliminate, clock risk.
- DSSE does not identify the signature algorithm. A Python standard-library-only verifier
  has hashes and HMAC but no general asymmetric signature verification. HMAC would let
  every verifier forge attestations and is not suitable across trust domains. Production
  deployments need a pinned asymmetric verifier or a trusted external verification
  service.

## Normative references

- [DSSE protocol](https://github.com/secure-systems-lab/dsse/blob/master/protocol.md)
- [DSSE envelope schema](https://github.com/secure-systems-lab/dsse/blob/master/envelope.proto)
- [in-toto Statement v1](https://github.com/in-toto/attestation/blob/main/spec/v1/statement.md)
- [in-toto envelope v1](https://github.com/in-toto/attestation/blob/main/spec/v1/envelope.md)
- [in-toto ResourceDescriptor](https://github.com/in-toto/attestation/blob/main/spec/v1/resource_descriptor.md)
- [in-toto DigestSet](https://github.com/in-toto/attestation/blob/main/spec/v1/digest_set.md)
