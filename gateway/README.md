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
broker with a real source bundle and ephemeral ECDSA key, then probes exact environment,
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

A supervised adapter hands one `StoredExecution` to the linked child as a directory with
exactly these files:

```
request.json
capability.dsse.json
receipt.dsse.json
output.bundle
stdout
stderr
```

The first three are `StoredExecution.request`, `.capability`, and `.receipt` byte for byte.
The remaining files are the content-store bytes referenced by the `outputBundle`, `stdout`,
and `stderr` artifacts. Do not include `sourceBundle`, the gateway validation record, a
trusted profile, or a verification key. The child reads its authoritative source bundle
from the frozen source session and receives profile and key through separate host-controlled
paths.

The adapter must create a real private directory and fixed regular files without symlinks
or hard links, then stop writing before the consumer begins. The linked consumer still
verifies every byte, both signatures, and all trusted expected context independently. It
stores the accepted evidence immutably, creates an exact local commit only after those
checks, and compare-and-swap updates its generated local branch. That consumer requires Git
2.36 or newer plus exclusive controller access to the target repository while linking or
landing. Its session directories and Git metadata must remain on private local POSIX storage
that gateway jobs and untrusted concurrent writers cannot modify. Neither the adapter nor
the gateway checks out that branch, pushes it, opens a pull request, or publishes a review.
