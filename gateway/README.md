# Underwrite execution gateway

This package is the privileged host boundary for one version 1 Underwrite execution. It
verifies a frozen source bundle, prepares an isolated Linux container, issues a short-lived
host capability, runs one exact job, verifies every returned artifact, signs the linked
receipt, and commits the evidence to a replay-protected host store.

It remains separate from every source review or report session. Those sessions stay
`no_exec`; accepting a finding does not authorize execution. A separately approved,
one-finding child may submit its reserved request to this gateway and consume the returned
evidence under `gateway_attested` policy. A receipt is evidence, not permission to apply a
bundle, push a branch, publish a review, or cause another external effect.

## Runtime profile

Version 1 uses a pinned Linux Docker image and one unprivileged target process. The trusted
PID 1 wrapper is the only process that retains the narrow capabilities needed to prepare
the workspace, drop identity, and terminate the target. Before `execve`, it confirms the
target has no inheritable, permitted, effective, or ambient capabilities and installs a
second seccomp filter that denies networking, process creation, and `io_uring`.

The container has no host mounts, no Docker socket, no network namespace interfaces beyond
an unusable loopback device, a read-only root, and a bounded `noexec` tmpfs workspace. The
target receives closed standard input and exactly the configured environment. Git is not
installed in the runtime image. Source and output Git operations happen on the trusted host
in fresh quarantines with hooks, filters, inherited configuration, alternates, replacement
objects, partial clones, prerequisites, and submodules rejected.

Every Git subprocess runs through the bundled resource limiter with a shared operation
deadline. Fetched bundles stay packed, command output is spooled and bounded, and the
quarantine is measured against an aggregate disk limit before and after full object
verification.

The single-process profile is intentional. A policy with `processes` other than `1` is not
accepted by this implementation.

## Build and verify

Build from the pinned multi-platform Python base, then configure `GatewayPolicy.image` with
the resulting platform-specific image ID. Runtime policy never accepts a tag.

```bash
docker build \
  --build-arg PYTHON_IMAGE=python:3.13-slim@sha256:7ce4b6dfe35e55397b7cda544f8a13f191b7ae28dc5aad71fe664dbc9bc2623f \
  --file gateway/Dockerfile \
  --tag underwrite-gateway:local \
  .

docker image inspect --format='{{.Id}}' underwrite-gateway:local
```

The live conformance suite is opt-in locally and mandatory in Linux CI:

```bash
UNDERWRITE_LIVE_GATEWAY_IMAGE=sha256:<final-image-id> \
UNDERWRITE_LIVE_GATEWAY_PLATFORM=linux/amd64 \
UNDERWRITE_LIVE_GATEWAY_DOCKER_HOST=unix:///var/run/docker.sock \
python -m unittest tests.test_gateway_live -v
```

The suite fails when Docker or daemon seccomp support is missing. It exercises the real
one-shot adapter and broker with a real source bundle and ephemeral ECDSA key, then probes exact environment,
closed stdin, zero target capabilities, denied network and process syscalls, denied host
writes, denied workspace execution, teardown, output capture, timeouts, replay, output
quarantine, receipt verification, and evidence persistence.

## Host configuration

The host needs Docker Engine with cgroups and an active seccomp profile, Git with SHA-256
object-format support, and OpenSSL. Keep the ECDSA P-256 private key in a host-only regular
file with no group or other permissions. Provision it with P-256 named-curve encoding.
`OpenSSLSigner` derives its key ID from the public key, enforces that exact
`prime256v1` profile, and verifies the key pair during startup.

Provision the linked application side with the public key only. `OpenSSLVerifier` enforces
the same P-256 profile, derives the same SHA-256 key ID, and never reads the private key.
Keep its trusted profile and pinned public key outside repositories, sessions, jobs, and
returned evidence. The profile binds the expected key ID, signer, executor, job, sandbox,
and exit code used to verify each child request.

Run the gateway and linked consumer as distinct OS users. Put only those two trusted users
in one dedicated evidence handoff group and configure its numeric ID as `consumerGid`.
Never add a target job account, container runtime identity, repository user, or other
untrusted principal to that group. The consumer account must not be able to read the
gateway configuration, private key, store, or temporary files.

The trusted computing base includes the dedicated gateway OS account, host kernel and
clock, container runtime, Docker daemon and its administrators, protected local Docker
socket, store filesystem, signing key, and the trusted Python, Git, OpenSSL, and gateway
programs and libraries. Docker access is root-equivalent. No untrusted principal may
control the daemon, socket, gateway account, clock, binaries, or store. Binding a Unix
socket path into the executor ID identifies configured routing only; it does not
authenticate the daemon behind that path.

Run each gateway deployment in a fresh dedicated process. Do not embed the broker in a web
server, worker pool, controller, or any process with unrelated work. The first broker claim
requires a single-threaded process and installs a process-wide soft address-space budget.
Linux requires the process baseline to fit below an absolute 768 MiB ceiling. Darwin
measures the process baseline and allows 768 MiB above it because its shared mappings make
a fixed absolute ceiling unusable. The trusted Docker client restores only its inherited
soft address-space limit to the unchanged hard ceiling before startup because its Go
runtime reserves a large virtual arena. Git instead runs under its separate fixed hard
address-space profile. These exact modes and the Docker exemption are bound into the
executor ID. The broker then permits one execution at a time.
Construction must acknowledge that process contract explicitly:

```python
broker = ExecutionBroker(
    policy,
    signer,
    store_root,
    DockerRunner(
        policy.image,
        policy.platform,
        policy.docker_host,
        policy.deployment_id,
        policy.runtime_domain_id,
    ),
    dedicated_process=True,
)
```

Version 1 accepts at most a 64 MiB source bundle, 64 MiB workspace, 16 MiB of combined
captured output, 2 GiB of target memory, and 900 seconds each of wall and CPU time. CPU time
cannot exceed wall time. The fixed tree profile permits at most 20,000 entries and 8 MiB of
aggregate path bytes, with each path limited to 4,096 bytes and 256 components. Deploy more
supervised gateway processes for concurrency rather than raising these limits in an
embedding process. Version 1 does not scale out one trust domain: every worker that can
receive the same sessions or challenges must share one authoritative store and replay
ledger, the same Docker daemon, and the same `runtime_domain_id`. That ID names the stable
store-and-daemon pairing and must remain unchanged across deployment, key, code, image, and
executable revisions. The broker derives one fixed container name from it. While holding
the store execution lease, every attempt force-removes that name, proves it absent, and only
then performs replay lookup or starts new work. This recovers containers left by a crashed
or replaced deployment without relying on process memory.

Separate stores are safe only for disjoint upstream authorization domains that cannot
receive the same session or challenge. Give each such store-and-daemon pairing a unique
`runtime_domain_id`, including pairings that use the same daemon. Version 1 does not support
remote failover of an active runtime domain. Before changing `runtime_domain_id` or moving
the store to another host or daemon, stop intake, drain active work, reconcile the old fixed
container name on the old daemon, and prove it absent.

Give each deployment revision an operator-controlled `deployment_id` and an
explicit local Unix Docker endpoint such as `unix:///var/run/docker.sock`. The runner ignores ambient
Docker contexts, hosts, client configuration, and home directories. Policy accepts only
the fixed credential-free environment entries implemented by the gateway. The executor ID
binds the deployment, Docker endpoint, host implementation digest, platform-specific image
ID, runtime wrapper digest, executable, environment, sandbox, and host resource profile.
Rotate `deployment_id` whenever any trusted component or administrator boundary changes,
including the host, daemon, socket routing, account, store, clock source, key, or binary and
library set.

Use an absolute private directory for the content and replay store. The authoritative
SQLite ledger and content-addressed objects must remain outside repositories and target
workspaces. Place the ledger, staging area, and object store on one local POSIX filesystem
that provides process-shared `flock`, SQLite WAL locking, atomic hard-link and rename
operations, and durable file and directory `fsync`. Network and userspace filesystems are
unsupported. Version 1 has no garbage collector or retention service, and a unique execution
can retain roughly 208 MiB at the maximum source, output-bundle, and stream limits. Put the
store on a dedicated capacity-limited filesystem, monitor free space, and stop intake before
exhaustion. Any future archival policy must preserve the replay and challenge tombstones
even if bulky completed artifacts move to separately verified storage. A caller must build
`GatewayPolicy` entirely from trusted host configuration and must build each execution
request from the authoritative frozen session and queued action. Never copy signer,
executor, sandbox, target, job, or expected exit values from target code or a returned
envelope.

`ExecutionBroker.execute(request, source_bundle_path)` returns a `StoredExecution` only
after the linked receipt and every artifact have been independently verified and committed.
Any launch, limit, teardown, signing, replay, or verification ambiguity fails closed and
produces no conforming receipt. Cancellation and close verify that the container is absent,
and successful absence verification happens before receipt signing and commit.
An ambiguous teardown permanently poisons the gateway process, and the broker refuses all
later requests until its supervisor replaces that dedicated process. The replacement
process reconciles the stable runtime container under the shared store lease before it can
return replayed evidence or begin another execution.

## One-shot supervised adapter

`gateway/adapter.py` is the reference host adapter. Each invocation loads one private
configuration, constructs one broker inside that dedicated process, submits one reserved
child request with its authoritative source bundle, publishes one evidence directory, and
exits. The configuration is version 1 with exactly these fields:

```json
{
  "version": 1,
  "privateKey": "/absolute/path/signing-private.pem",
  "publicKey": "/absolute/path/signing-public.pem",
  "storeRoot": "/absolute/private/gateway-store",
  "docker": "/absolute/path/to/docker",
  "openssl": "/absolute/path/to/openssl",
  "git": "/absolute/path/to/git",
  "deploymentId": "urn:underwrite:gateway:deployment",
  "runtimeDomainId": "urn:underwrite:runtime-domain:host-a",
  "dockerHost": "unix:///var/run/docker.sock",
  "image": "sha256:<64 lowercase hex characters>",
  "platform": "linux/amd64",
  "runnerSha256": "<64 lowercase hex characters>",
  "targetUid": 65532,
  "targetGid": 65532,
  "consumerGid": 2001,
  "capabilitySeconds": 300,
  "maxSourceBundleBytes": 67108864,
  "profile": {
    "version": 1,
    "keyId": "sha256:<64 lowercase hex characters>",
    "signerId": "urn:underwrite:signer:host-a",
    "executorId": "urn:underwrite:executor:docker-single-process-v1:<digest>",
    "job": {
      "argv": ["/usr/local/bin/python3", "-I", "-S", "/opt/underwrite/implement.py"],
      "cwd": ".",
      "environment": {"LANG": "C", "TZ": "UTC"},
      "executable": {
        "path": "/usr/local/bin/python3",
        "sha256": "<64 lowercase hex characters>",
        "bytes": 1
      },
      "stdin": "closed"
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
        "wallSeconds": 60,
        "cpuSeconds": 60,
        "memoryBytes": 134217728,
        "processes": 1,
        "workspaceBytes": 67108864,
        "outputBytes": 1048576
      }
    },
    "exitCode": 0
  }
}
```

The embedded `profile` is the exact separately provisioned profile later given to
`implementationctl.py`. Its key ID must match the configured key pair, and its executor ID
must match the policy derived from this deployment. The request's complete `job`, `sandbox`,
and `exitCode` must match it. The `docker`, `openssl`, and `git` values are explicit absolute
host executable paths. They must be trusted regular executables owned by root or the gateway
account and not writable by group or other. The job executable is instead an absolute path
inside the pinned runtime image.

Preprovision `storeRoot` as a gateway-owned `0700` directory. Separately, provision the
immediate outbox parent as gateway-owned, group-owned by `consumerGid`, and exactly `0710`.
Every outbox ancestor must either reject group and other writes or be a sticky shared
ancestor, and it must be traversable by the consumer group or by other users. The adapter
publishes a gateway-owned `0750` final directory containing six gateway-owned,
consumer-group `0640` files. The outbox's lack of
read permission prevents directory listing, so the supervisor must give the consumer the
exact final path. Keep the configuration, private key, store, and temporary root outside
the shared group path. All host state and the outbox must be on supported local POSIX
storage outside repositories and target workspaces. The trusted supervisor selects every
argument; never take an adapter path or trusted profile value from target code.

Install the gateway and Python interpreter beneath root-owned or gateway-owned directory
ancestry that is not writable by group or other. A sticky shared parent such as `/tmp` is
permitted only when every descendant on the selected path is still owned by root or the
gateway account. Apply the same rule to every configured executable and data path.

Run the adapter as a fresh isolated Python process:

```bash
/absolute/path/to/python3 -I -S /absolute/path/gateway/adapter.py \
  --config /srv/underwrite-gateway/private/gateway.json \
  --request /srv/underwrite-gateway/private/request.json \
  --source-bundle /srv/underwrite-gateway/private/pr.bundle \
  --evidence-dir /srv/underwrite-handoff/outbox/attempt-1
```

Handled outcomes write exactly one JSON object to stdout. A completed handoff exits zero:

```json
{"evidenceDir":"/srv/underwrite-handoff/outbox/attempt-1","status":"complete","version":1}
```

A recoverable or ambiguous outcome exits one:

```json
{"reason":"retry the same request and source bundle","status":"retry","version":1}
```

A replay identity that can no longer produce a receipt exits two:

```json
{"reason":"gateway attempt is terminal without a receipt","status":"failed","version":1}
```

Only the exact `failed` result paired with exit code two authorizes the controller to call
`implementationctl.py fail` for that child attempt and then request a fresh challenge. A
`retry` result, missing or malformed stdout, process interruption, unknown exit code, or any
other ambiguous outcome requires the same request and source bundle to be retried. A
`complete` result supplies the exact evidence directory to `implementationctl.py consume`.
The adapter never invokes `fail`, `consume`, or `land` itself.

A completed evidence directory contains exactly:

```
request.json
capability.dsse.json
receipt.dsse.json
output.bundle
stdout
stderr
```

The first three are `StoredExecution.request`, `.capability`, and `.receipt` byte for byte.
The remaining files are the content-store bytes referenced by `outputBundle`, `stdout`, and
`stderr`. The adapter never exports `sourceBundle`, the gateway validation record, the
profile, or a key. It writes the files under a private deterministic staging name, makes
them durable, atomically renames the complete directory into place, and only then grants
the consumer group read access. Repeating an exact completed request returns the stored
broker result. An existing evidence directory is accepted only when all six files are
byte-identical and its parent directory has passed the durability barrier. A retry holding
the parent lock removes only the staging directory derived from that exact final name. It
also repairs a complete `0700` final directory left by interruption before group handoff.

If publication fails after the broker commits a result, retry the same request and source
bundle to recover the stored result. The linked consumer independently verifies every byte,
both signatures, and all trusted expected context before it persists evidence or changes a
local branch. After `implementationctl.py consume` durably succeeds, the gateway-owner
supervisor may delete the final transport directory. The consumer cannot and must not
delete it through the `0710` parent. Neither the adapter nor the gateway checks out the
branch, pushes it, opens a pull request, or publishes a review.
