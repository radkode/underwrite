"""Trusted orchestration for one Underwrite execution attempt."""

import copy
import fcntl
import hashlib
import json
import os
import platform as host_platform_module
import posixpath
import re
import resource
import stat
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from skills.underwrite.scripts import execution_receipt

from . import artifacts
from . import git_limiter
from .store import ArtifactRef, ContentStore, ReplayConflict, ReplayLedger, StoreError


GATEWAY_VERSION = 1
BACKEND = "docker-single-process-v1"
SIGNING_ALGORITHM = "ecdsa-p256-sha256"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_INTEGER = 9_007_199_254_740_991
_MAX_LINUX_ID = 4_294_967_294
_MIN_MEMORY_BYTES = 64 * 1024 * 1024
_MAX_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
_MAX_SOURCE_BUNDLE_BYTES = 64 * 1024 * 1024
_MAX_WORKSPACE_BYTES = 64 * 1024 * 1024
_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
_MAX_WALL_SECONDS = 900
_MAX_CPU_SECONDS = 900
_HOST_MEMORY_BUDGET_BYTES = 768 * 1024 * 1024
_HOST_EXECUTION_WAIT_SECONDS = 30
_EXECUTION_SLOT = threading.BoundedSemaphore(1)
_PROCESS_CLAIM_LOCK = threading.Lock()
_CLAIMED_PROCESS_ID = None
_SAFE_ENVIRONMENT = {
    "LANG": frozenset(("C", "C.UTF-8", "POSIX")),
    "LC_ALL": frozenset(("C", "C.UTF-8", "POSIX")),
    "PATH": frozenset(("/usr/local/bin:/usr/bin:/bin",)),
    "PYTHONDONTWRITEBYTECODE": frozenset(("1",)),
    "PYTHONUTF8": frozenset(("1",)),
    "TZ": frozenset(("UTC",)),
}


class GatewayError(RuntimeError):
    """The request cannot produce trusted execution evidence."""


class ExecutionFailed(GatewayError):
    """The sandbox ran or prepared, but no conforming receipt can be issued."""


class _FrozenDict(dict):
    def __init__(self, value):
        dict.__init__(
            self,
            {
                key: _FrozenDict(item) if isinstance(item, dict) else item
                for key, item in value.items()
            },
        )

    def _immutable(self, *_args, **_kwargs):
        raise TypeError("gateway policy mappings are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable

    def __deepcopy__(self, memo):
        return copy.deepcopy(dict(self), memo)


def _canonical_bytes(value):
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise GatewayError("gateway value is not canonical JSON") from error


def _implementation_sha256():
    root = Path(__file__).resolve().parent
    paths = {
        name: root / name
        for name in (
            "artifacts.py",
            "broker.py",
            "docker_client.py",
            "docker_runner.py",
            "git_limiter.py",
            "signing.py",
            "store.py",
        )
    }
    paths["execution_receipt.py"] = Path(execution_receipt.__file__).resolve()
    try:
        manifest = {
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in paths.items()
        }
    except OSError as error:
        raise GatewayError("host gateway implementation cannot be measured") from error
    return hashlib.sha256(_canonical_bytes(manifest)).hexdigest()


def _enforce_host_memory_budget():
    if sys.platform == "darwin":
        maximum = git_limiter._darwin_address_space_limit(
            _HOST_MEMORY_BUDGET_BYTES
        )
    else:
        maximum = _HOST_MEMORY_BUDGET_BYTES
        if _linux_address_space_bytes() >= maximum:
            raise GatewayError(
                "gateway exceeds the Linux address-space baseline"
            )
    try:
        _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        value = maximum if hard == resource.RLIM_INFINITY else min(maximum, hard)
        resource.setrlimit(resource.RLIMIT_AS, (value, hard))
    except (OSError, ValueError) as error:
        raise GatewayError("gateway address-space budget cannot be applied") from error


def _linux_address_space_bytes():
    try:
        with open("/proc/self/statm", "rb") as handle:
            value = handle.read(128)
        fields = value.split()
        if not fields or len(value) == 128 or not fields[0].isdigit():
            raise ValueError
        return int(fields[0]) * resource.getpagesize()
    except (OSError, ValueError, OverflowError) as error:
        raise GatewayError(
            "gateway Linux address-space baseline cannot be measured"
        ) from error


def _host_platform():
    systems = {"linux": "linux", "darwin": "darwin"}
    machines = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }
    system = systems.get(sys.platform)
    machine = machines.get(host_platform_module.machine().lower())
    if system is None or machine is None:
        raise GatewayError("gateway host platform is unsupported")
    return f"{system}/{machine}"


def _claim_dedicated_process():
    global _CLAIMED_PROCESS_ID
    process_id = os.getpid()
    with _PROCESS_CLAIM_LOCK:
        if _CLAIMED_PROCESS_ID == process_id:
            return
        if _CLAIMED_PROCESS_ID is not None or threading.active_count() != 1:
            raise GatewayError(
                "gateway must claim a new single-threaded dedicated process"
            )
        _enforce_host_memory_budget()
        _CLAIMED_PROCESS_ID = process_id


def _text(value, label):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise GatewayError(f"{label} must be non-empty text")
    return value


def _positive(value, label):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 < value <= _MAX_INTEGER
    ):
        raise GatewayError(f"{label} must be a positive integer")
    return value


def _nonnegative(value, label):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _MAX_INTEGER
    ):
        raise GatewayError(f"{label} must be a non-negative integer")
    return value


def _exact(value, fields, label):
    if not isinstance(value, dict):
        raise GatewayError(f"{label} must be an object")
    missing = sorted(fields - set(value))
    unknown = sorted(set(value) - fields)
    if missing:
        raise GatewayError(f"{label} is missing {', '.join(missing)}")
    if unknown:
        raise GatewayError(f"{label} has unknown field {', '.join(unknown)}")
    return value


def _stream_descriptor(data):
    if not isinstance(data, bytes):
        raise GatewayError("captured stream must be bytes")
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "truncated": False,
    }


def _absolute_path(value, label):
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
        or "\x00" in value
        or "\\" in value
        or posixpath.normpath(value) != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise GatewayError(f"{label} must be a normalized absolute POSIX path")
    return value


def _docker_host(value):
    if not isinstance(value, str) or not value.startswith("unix://"):
        raise GatewayError("Docker host must be an explicit local Unix socket")
    path = value[len("unix://"):]
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "\x00" in path
        or "\\" in path
        or posixpath.normpath(path) != path
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise GatewayError("Docker host must be a normalized absolute Unix socket path")
    return value


@dataclass(frozen=True)
class GatewayPolicy:
    signer_id: str
    deployment_id: str
    runtime_domain_id: str
    docker_host: str
    image: str
    platform: str
    runner_sha256: str
    executable: dict
    environment: dict
    sandbox: dict
    target_uid: int
    target_gid: int
    capability_seconds: int = 300
    max_source_bundle_bytes: int = 67_108_864
    host_implementation_sha256: str = field(init=False)
    host_platform: str = field(init=False)

    def __post_init__(self):
        _text(self.signer_id, "signer ID")
        for value, label in (
            (self.deployment_id, "deployment ID"),
            (self.runtime_domain_id, "runtime domain ID"),
        ):
            value = _text(value, label)
            try:
                encoded = value.encode("utf-8")
            except UnicodeEncodeError as error:
                raise GatewayError(f"{label} is invalid") from error
            if len(encoded) > 512 or any(
                ord(character) < 32 or ord(character) == 127
                for character in value
            ):
                raise GatewayError(f"{label} is invalid")
        _docker_host(self.docker_host)
        if not isinstance(self.image, str) or not _IMAGE.fullmatch(self.image):
            raise GatewayError("runtime image must be an immutable sha256 ID")
        if self.platform not in ("linux/amd64", "linux/arm64"):
            raise GatewayError("runtime platform must be linux/amd64 or linux/arm64")
        if (
            not isinstance(self.runner_sha256, str)
            or not _SHA256.fullmatch(self.runner_sha256)
        ):
            raise GatewayError("runner SHA-256 is invalid")
        _positive(self.target_uid, "target UID")
        _positive(self.target_gid, "target GID")
        if self.target_uid > _MAX_LINUX_ID or self.target_gid > _MAX_LINUX_ID:
            raise GatewayError("target identity is outside the Linux ID range")
        _positive(self.capability_seconds, "capability lifetime")
        if self.capability_seconds > 300:
            raise GatewayError("capability lifetime must be from 1 to 300 seconds")
        _positive(self.max_source_bundle_bytes, "source bundle limit")
        if self.max_source_bundle_bytes > _MAX_SOURCE_BUNDLE_BYTES:
            raise GatewayError("source bundle limit exceeds the fixed v1 host bound")
        executable = _exact(
            self.executable, {"path", "sha256", "bytes"}, "policy executable"
        )
        _absolute_path(executable["path"], "policy executable path")
        if not isinstance(executable["sha256"], str) or not _SHA256.fullmatch(
            executable["sha256"]
        ):
            raise GatewayError("policy executable SHA-256 is invalid")
        _positive(executable["bytes"], "policy executable bytes")
        if not isinstance(self.environment, dict):
            raise GatewayError("policy environment must be an object")
        for name, value in self.environment.items():
            _text(name, "environment name")
            if "=" in name or not isinstance(value, str) or "\x00" in value:
                raise GatewayError("policy environment entry is invalid")
            if value not in _SAFE_ENVIRONMENT.get(name, ()):
                raise GatewayError(
                    "policy environment must use only fixed credential-free entries"
                )
        sandbox = _exact(
            self.sandbox,
            {
                "policy",
                "credentials",
                "network",
                "hostWrites",
                "gitHooks",
                "gitFilters",
                "timeout",
                "limits",
            },
            "policy sandbox",
        )
        expected_literals = {
            "policy": execution_receipt.SANDBOX_POLICY_TYPE,
            "credentials": "absent",
            "network": "denied",
            "hostWrites": "denied",
            "gitHooks": "disabled",
            "gitFilters": "disabled",
            "timeout": "enforced",
        }
        if any(sandbox[name] != value for name, value in expected_literals.items()):
            raise GatewayError("policy sandbox does not make every v1 isolation claim")
        limits = _exact(
            sandbox["limits"],
            {
                "wallSeconds",
                "cpuSeconds",
                "memoryBytes",
                "processes",
                "workspaceBytes",
                "outputBytes",
            },
            "policy sandbox limits",
        )
        for name, value in limits.items():
            _positive(value, f"policy sandbox {name}")
        if limits["processes"] != 1:
            raise GatewayError("the v1 Docker profile requires processes to equal 1")
        if limits["memoryBytes"] < _MIN_MEMORY_BYTES:
            raise GatewayError("the v1 Docker profile requires at least 64 MiB of memory")
        if limits["memoryBytes"] > _MAX_MEMORY_BYTES:
            raise GatewayError("sandbox memory exceeds the fixed v1 host bound")
        if limits["wallSeconds"] > _MAX_WALL_SECONDS:
            raise GatewayError("sandbox wall time exceeds the fixed v1 host bound")
        if limits["cpuSeconds"] > _MAX_CPU_SECONDS:
            raise GatewayError("sandbox CPU time exceeds the fixed v1 host bound")
        if limits["cpuSeconds"] > limits["wallSeconds"]:
            raise GatewayError("sandbox CPU time cannot exceed wall time")
        if limits["workspaceBytes"] > _MAX_WORKSPACE_BYTES:
            raise GatewayError("workspace limit exceeds the fixed v1 host bound")
        if limits["outputBytes"] > _MAX_OUTPUT_BYTES:
            raise GatewayError("captured output exceeds the fixed v1 host bound")
        object.__setattr__(self, "executable", _FrozenDict(self.executable))
        object.__setattr__(self, "environment", _FrozenDict(self.environment))
        object.__setattr__(self, "sandbox", _FrozenDict(self.sandbox))
        object.__setattr__(self, "host_implementation_sha256", _implementation_sha256())
        object.__setattr__(self, "host_platform", _host_platform())

    @property
    def executor_manifest(self):
        container_name = "underwrite-" + hashlib.sha256(
            self.runtime_domain_id.encode("utf-8")
        ).hexdigest()
        return copy.deepcopy({
            "version": GATEWAY_VERSION,
            "backend": BACKEND,
            "deploymentId": self.deployment_id,
            "runtimeDomainId": self.runtime_domain_id,
            "dockerHost": self.docker_host,
            "hostImplementationSha256": self.host_implementation_sha256,
            "image": self.image,
            "platform": self.platform,
            "runnerSha256": self.runner_sha256,
            "signingAlgorithm": SIGNING_ALGORITHM,
            "executable": self.executable,
            "environment": self.environment,
            "sandbox": self.sandbox,
            "targetUser": {"uid": self.target_uid, "gid": self.target_gid},
            "outputRef": artifacts.OUTPUT_REF,
            "hostResources": {
                "dedicatedProcess": True,
                "hostPlatform": self.host_platform,
                "addressSpace": {
                    "enforcement": "soft-rlimit",
                    "mode": (
                        "additional-to-measured-baseline"
                        if self.host_platform.startswith("darwin/")
                        else "absolute"
                    ),
                    "bytes": _HOST_MEMORY_BUDGET_BYTES,
                    "trustedChildren": {
                        "dockerClient": {
                            "mode": "restore-soft-to-inherited-hard",
                        },
                        "git": {
                            "enforcement": "hard-rlimit",
                            "mode": (
                                "additional-to-measured-baseline"
                                if self.host_platform.startswith("darwin/")
                                else "absolute"
                            ),
                            "bytes": git_limiter._MEMORY_BYTES,
                        },
                    },
                },
                "concurrentExecutions": 1,
                "concurrencyScope": "runtime-domain-store-and-container-name",
                "containerName": container_name,
                "queueSeconds": _HOST_EXECUTION_WAIT_SECONDS,
                "sourceBundleBytes": _MAX_SOURCE_BUNDLE_BYTES,
                "workspaceBytes": _MAX_WORKSPACE_BYTES,
                "capturedOutputBytes": _MAX_OUTPUT_BYTES,
            },
            "isolation": {
                "containerSeccomp": "docker-builtin",
                "targetSeccomp": "no-network-no-process-creation-v1",
                "trustedWrapperCapabilities": [
                    "CHOWN",
                    "DAC_READ_SEARCH",
                    "KILL",
                    "SETGID",
                    "SETUID",
                ],
                "workspace": "size-limited-tmpfs",
            },
        })

    @property
    def executor_id(self):
        digest = hashlib.sha256(_canonical_bytes(self.executor_manifest)).hexdigest()
        return f"urn:underwrite:executor:{BACKEND}:{digest}"


class ExecutionRequest:
    FIELDS = {
        "version",
        "sessionId",
        "challenge",
        "target",
        "action",
        "job",
        "sandbox",
        "exitCode",
    }

    def __init__(self, value, policy):
        value = copy.deepcopy(_exact(value, self.FIELDS, "execution request"))
        if type(value["version"]) is not int or value["version"] != 1:
            raise GatewayError("execution request version must be 1")
        try:
            parsed = uuid.UUID(value["sessionId"])
        except (ValueError, AttributeError) as error:
            raise GatewayError("execution session ID must be a UUID") from error
        if str(parsed) != value["sessionId"]:
            raise GatewayError("execution session ID must be canonical")
        if not isinstance(value["challenge"], str) or not _SHA256.fullmatch(
            value["challenge"]
        ):
            raise GatewayError("execution challenge must be 32 lowercase hex bytes")
        action = _exact(value["action"], {"seq", "beat", "attempt"}, "action")
        for name, coordinate in action.items():
            _positive(coordinate, f"action {name}")
        job = _exact(
            value["job"],
            {"argv", "cwd", "environment", "executable", "stdin"},
            "job",
        )
        if not isinstance(job["argv"], list) or not job["argv"]:
            raise GatewayError("job argv must be a non-empty array")
        if any(not isinstance(argument, str) or "\x00" in argument for argument in job["argv"]):
            raise GatewayError("job argv contains an invalid argument")
        if job["environment"] != policy.environment:
            raise GatewayError("job environment does not match trusted policy")
        if job["executable"] != policy.executable:
            raise GatewayError("job executable does not match trusted policy")
        if job["argv"][0] != policy.executable["path"]:
            raise GatewayError("job argv[0] does not match the trusted executable")
        if job["stdin"] != "closed":
            raise GatewayError("job standard input must be closed")
        if value["sandbox"] != policy.sandbox:
            raise GatewayError("request sandbox does not match trusted policy")
        _nonnegative(value["exitCode"], "expected exit code")
        try:
            execution_receipt.build_host_capability_payload(
                {
                    "signerId": policy.signer_id,
                    "executorId": policy.executor_id,
                    "sessionId": value["sessionId"],
                    "challenge": value["challenge"],
                    "target": value["target"],
                    "action": value["action"],
                    "job": value["job"],
                    "inputTree": "0" * 64,
                    "sandbox": value["sandbox"],
                },
                datetime(2000, 1, 1, tzinfo=timezone.utc),
                datetime(2000, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
            )
        except execution_receipt.ReceiptError as error:
            raise GatewayError("execution request violates the v1 protocol") from error
        self.value = value

    @property
    def bytes(self):
        return _canonical_bytes(self.value)


class ExecutionBroker:
    """Run one gateway deployment inside an explicitly dedicated process."""

    def __init__(
        self,
        policy,
        signer,
        store_root,
        runner,
        clock=None,
        *,
        dedicated_process,
    ):
        if dedicated_process is not True:
            raise GatewayError("broker requires an explicit dedicated-process claim")
        if not isinstance(policy, GatewayPolicy):
            raise GatewayError("broker requires a GatewayPolicy")
        if signer.signer_id != policy.signer_id:
            raise GatewayError("signer identity does not match gateway policy")
        if getattr(signer, "algorithm", None) != SIGNING_ALGORITHM:
            raise GatewayError("signer algorithm does not match gateway policy")
        if not callable(getattr(runner, "prepare", None)):
            raise GatewayError("runner must provide prepare()")
        if not callable(getattr(runner, "reconcile", None)):
            raise GatewayError("runner must provide reconcile()")
        if getattr(runner, "poisoned", False):
            raise GatewayError("runner is poisoned by ambiguous teardown")
        if (
            getattr(runner, "image", None) != policy.image
            or getattr(runner, "platform", None) != policy.platform
            or getattr(runner, "docker_host", None) != policy.docker_host
            or getattr(runner, "deployment_id", None) != policy.deployment_id
            or getattr(runner, "runtime_domain_id", None)
            != policy.runtime_domain_id
        ):
            raise GatewayError(
                "runner deployment, runtime domain, image, platform, and Docker host "
                "must match gateway policy"
            )
        _claim_dedicated_process()
        self.policy = policy
        self.signer = signer
        self.content = ContentStore(Path(store_root))
        self.ledger = ReplayLedger(self.content)
        self.runner = runner
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _acquire_store_lease(self):
        path = self.content.root / "execution.lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        if not hasattr(os, "O_NOFOLLOW"):
            raise GatewayError("gateway host requires no-follow file opens")
        flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as error:
            raise GatewayError("gateway execution lease cannot be opened") from error
        try:
            os.fchmod(descriptor, 0o600)
            details = os.fstat(descriptor)
            current = os.stat(path, follow_symlinks=False)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_nlink != 1
                or details.st_dev != current.st_dev
                or details.st_ino != current.st_ino
            ):
                raise GatewayError("gateway execution lease is not a private regular file")
            deadline = time.monotonic() + _HOST_EXECUTION_WAIT_SECONDS
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return descriptor
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise GatewayError(
                            "gateway deployment execution capacity is unavailable"
                        )
                    time.sleep(min(0.05, remaining))
        except Exception:
            os.close(descriptor)
            raise

    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise GatewayError("gateway clock must return an aware datetime")
        return value.astimezone(timezone.utc)

    def _identity(self, request):
        job_digest = execution_receipt.canonical_sha256(request.value["job"])
        identity = {
            "signerId": self.policy.signer_id,
            "executorId": self.policy.executor_id,
            "sessionId": request.value["sessionId"],
            "challenge": request.value["challenge"],
            "seq": request.value["action"]["seq"],
            "beat": request.value["action"]["beat"],
            "attempt": request.value["action"]["attempt"],
            "jobDigest": job_digest,
        }
        replay_key = hashlib.sha256(_canonical_bytes(identity)).hexdigest()
        challenge = {
            "sessionId": request.value["sessionId"],
            "challenge": request.value["challenge"],
        }
        challenge_key = hashlib.sha256(_canonical_bytes(challenge)).hexdigest()
        return replay_key, challenge_key

    def execute(self, request_value, source_bundle):
        if getattr(self.runner, "poisoned", False):
            raise GatewayError("runner is poisoned by ambiguous teardown")
        if not _EXECUTION_SLOT.acquire(timeout=_HOST_EXECUTION_WAIT_SECONDS):
            raise GatewayError("gateway host execution capacity is unavailable")
        lease = None
        try:
            lease = self._acquire_store_lease()
            try:
                self.runner.reconcile()
            except Exception as error:
                raise GatewayError(
                    "gateway could not reconcile its deployment container"
                ) from error
            return self._execute(request_value, source_bundle)
        finally:
            if lease is not None:
                fcntl.flock(lease, fcntl.LOCK_UN)
                os.close(lease)
            _EXECUTION_SLOT.release()

    def _execute(self, request_value, source_bundle):
        request = ExecutionRequest(request_value, self.policy)
        replay_key, challenge_key = self._identity(request)
        source_bytes = self.content.load_file(
            source_bundle, self.policy.max_source_bundle_bytes
        )
        source = ArtifactRef(hashlib.sha256(source_bytes).hexdigest(), len(source_bytes))
        existing = self.ledger.lookup_complete(replay_key, request.bytes, source)
        if existing is not None:
            return existing
        target = request.value["target"]
        if (
            source.sha256 != target["object_bundle_sha256"]
            or source.bytes != target["object_bundle_bytes"]
        ):
            raise GatewayError("source bundle bytes do not match the frozen target")

        prepared = None
        owns_reservation = False
        try:
            reserved = self.ledger.reserve(
                replay_key, challenge_key, request.bytes, source
            )
            if reserved is not None:
                return reserved
            owns_reservation = True
            with tempfile.TemporaryDirectory(
                prefix="underwrite-gateway-"
            ) as temporary_name:
                temporary = Path(temporary_name)
                workspace = temporary / "input"
                source_result = artifacts.verify_source_bundle(
                    source_bytes,
                    target,
                    workspace,
                    maximum_workspace_bytes=self.policy.sandbox["limits"][
                        "workspaceBytes"
                    ],
                )
                input_tree = source_result.git_tree
                archive = artifacts.pack_workspace(
                    workspace,
                    maximum_bytes=self.policy.sandbox["limits"]["workspaceBytes"],
                )
                published_source = self.content.put_bytes(source_bytes)
                if published_source != source:
                    raise GatewayError("published source does not match verified bytes")
                prepared = self.runner.prepare(
                    {
                        "version": 1,
                        "job": request.value["job"],
                        "sandbox": request.value["sandbox"],
                        "inputTree": input_tree,
                        "targetUid": self.policy.target_uid,
                        "targetGid": self.policy.target_gid,
                        "runnerSha256": self.policy.runner_sha256,
                    },
                    archive,
                )
                del archive
                del source_bytes
                ready = prepared.ready
                if ready.input_tree != input_tree:
                    raise GatewayError("sandbox input tree does not match quarantine")
                if ready.executable != self.policy.executable:
                    raise GatewayError("sandbox executable does not match trusted policy")
                if ready.runner_sha256 != self.policy.runner_sha256:
                    raise GatewayError("sandbox runner does not match trusted policy")
                common = self._expected(request, input_tree)
                issued_at = self._now()
                expires_at = issued_at + timedelta(
                    seconds=self.policy.capability_seconds
                )
                capability_payload = (
                    execution_receipt.build_host_capability_payload(
                        common, issued_at, expires_at
                    )
                )
                capability = execution_receipt.build_dsse_envelope(
                    capability_payload, self.signer.key_id, self.signer.sign
                )
                self.ledger.record_capability(replay_key, capability)
                self.ledger.begin_execution(replay_key)
                result = prepared.run(
                    hashlib.sha256(capability_payload).hexdigest(),
                    issued_at,
                    expires_at,
                )
                finished = prepared
                prepared = None
                finished.close()
                completed = self._complete(
                    request,
                    replay_key,
                    source,
                    capability,
                    capability_payload,
                    common,
                    result,
                    temporary,
                )
                return completed
        except Exception as error:
            if owns_reservation:
                try:
                    self.ledger.fail(
                        replay_key, str(error) or type(error).__name__
                    )
                except (StoreError, ReplayConflict):
                    pass
            if isinstance(error, (GatewayError, StoreError, ReplayConflict)):
                raise
            raise GatewayError("execution gateway failed closed") from error
        finally:
            if prepared is not None:
                prepared.close()

    def _expected(self, request, input_tree):
        return {
            "signerId": self.policy.signer_id,
            "executorId": self.policy.executor_id,
            "sessionId": request.value["sessionId"],
            "challenge": request.value["challenge"],
            "target": copy.deepcopy(request.value["target"]),
            "action": copy.deepcopy(request.value["action"]),
            "job": copy.deepcopy(request.value["job"]),
            "inputTree": input_tree,
            "sandbox": copy.deepcopy(request.value["sandbox"]),
        }

    def _complete(
        self,
        request,
        replay_key,
        source,
        capability,
        capability_payload,
        common,
        result,
        temporary,
    ):
        if result.status != "exited":
            raise ExecutionFailed(result.failure or "sandbox did not exit cleanly")
        if result.exit_code != request.value["exitCode"]:
            raise ExecutionFailed("sandbox exit code was not the trusted expected result")
        output_root = temporary / "output"
        output_tree = artifacts.unpack_workspace(
            result.workspace,
            output_root,
            maximum_bytes=self.policy.sandbox["limits"]["workspaceBytes"],
        )
        if output_tree != result.output_tree:
            raise ExecutionFailed("sandbox output tree does not match returned workspace")
        workspace_limit = self.policy.sandbox["limits"]["workspaceBytes"]
        bundle_limit = workspace_limit * 2
        output_bundle = artifacts.build_output_bundle(
            output_root,
            maximum_workspace_bytes=workspace_limit,
            maximum_bundle_bytes=bundle_limit,
        )
        if output_bundle.descriptor.git_tree != output_tree:
            raise ExecutionFailed(
                "output bundle tree does not match the measured workspace"
            )
        verified_bundle = artifacts.verify_output_bundle(
            output_bundle.bundle,
            maximum_workspace_bytes=workspace_limit,
            maximum_bundle_bytes=bundle_limit,
            expected=output_bundle.descriptor,
        )
        if verified_bundle != output_bundle.descriptor:
            raise ExecutionFailed("output bundle did not survive quarantine verification")
        stdout = _stream_descriptor(result.stdout)
        stderr = _stream_descriptor(result.stderr)
        if stdout["bytes"] + stderr["bytes"] > self.policy.sandbox["limits"]["outputBytes"]:
            raise ExecutionFailed("captured streams exceed the output limit")
        expected = dict(common)
        expected.update({
            "outputTree": output_tree,
            "outputBundle": {
                "sha256": output_bundle.descriptor.sha256,
                "bytes": output_bundle.descriptor.bytes,
            },
            "stdout": stdout,
            "stderr": stderr,
            "exitCode": result.exit_code,
        })
        receipt_payload = execution_receipt.build_execution_receipt_payload(
            capability_payload,
            expected,
            result.started_at,
            result.finished_at,
        )
        receipt = execution_receipt.build_dsse_envelope(
            receipt_payload, self.signer.key_id, self.signer.sign
        )
        verified = execution_receipt.verify_execution_receipt(
            capability,
            receipt,
            expected,
            self.signer.verify,
            self._now(),
        )
        output_reference = self.content.put_bytes(output_bundle.bundle)
        stdout_reference = self.content.put_bytes(result.stdout)
        stderr_reference = self.content.put_bytes(result.stderr)
        validation = _canonical_bytes({
            "version": 1,
            "status": "accepted",
            "signerId": verified.signer_id,
            "executorId": self.policy.executor_id,
            "capabilityPayloadSha256": hashlib.sha256(
                capability_payload
            ).hexdigest(),
            "receiptPayloadSha256": verified.payload_sha256,
            "inputTree": common["inputTree"],
            "outputTree": output_tree,
        })
        return self.ledger.complete(
            replay_key,
            receipt,
            {
                "sourceBundle": source,
                "outputBundle": output_reference,
                "stdout": stdout_reference,
                "stderr": stderr_reference,
            },
            validation,
        )
