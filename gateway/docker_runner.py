"""Host-side Docker transport for the isolated Linux runner."""

import hashlib
import json
import os
import posixpath
import re
import sys
import selectors
import shutil
import struct
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class DockerError(RuntimeError):
    """Docker could not establish or prove the required sandbox."""


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UTC_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
_FRAME = struct.Struct(">Q")
_MAX_CONTROL_BYTES = 1_000_000
_DOCKER_COMMAND_SECONDS = 30
_PROCESS_POISON_REASON = None


def _identity_sha256(value, label):
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
    ):
        raise DockerError(f"gateway {label} must be non-empty text")
    try:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    except UnicodeEncodeError as error:
        raise DockerError(f"gateway {label} must be UTF-8") from error


def runtime_container_name(runtime_domain_id):
    return "underwrite-" + _identity_sha256(
        runtime_domain_id, "runtime domain identity"
    )


def _duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise DockerError(f"duplicate runner field {key!r}")
        value[key] = item
    return value


def _json(data, label):
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_duplicates)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise DockerError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise DockerError(f"{label} must be an object")
    return value


def _write_frame(stream, data):
    if not isinstance(data, bytes):
        raise DockerError("runner frame must be bytes")
    try:
        stream.write(_FRAME.pack(len(data)))
        stream.write(data)
        stream.flush()
    except (OSError, ValueError) as error:
        raise DockerError("runner control channel closed while writing") from error


def _read_exact(stream, size, deadline):
    output = bytearray()
    selector = selectors.DefaultSelector()
    selector.register(stream, selectors.EVENT_READ)
    try:
        while len(output) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DockerError("runner control channel timed out")
            if not selector.select(remaining):
                raise DockerError("runner control channel timed out")
            chunk = os.read(stream.fileno(), size - len(output))
            if not chunk:
                raise DockerError("runner control channel ended early")
            output.extend(chunk)
    finally:
        selector.close()
    return bytes(output)


def _read_frame(stream, maximum, deadline):
    size = _FRAME.unpack(_read_exact(stream, _FRAME.size, deadline))[0]
    if size > maximum:
        raise DockerError("runner frame exceeds its configured byte limit")
    return _read_exact(stream, size, deadline)


def _timestamp(value, label):
    if not isinstance(value, str) or not _UTC_TIMESTAMP.fullmatch(value):
        raise DockerError(f"runner {label} is not a UTC timestamp")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise DockerError(f"runner {label} is not a real timestamp") from error


def _timestamp_text(value, label):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DockerError(f"runner {label} must be a timezone-aware datetime")
    value = value.astimezone(timezone.utc)
    timespec = "microseconds" if value.microsecond else "seconds"
    return value.isoformat(timespec=timespec).replace("+00:00", "Z")


def _docker_host(value):
    if not isinstance(value, str) or not value.startswith("unix://"):
        raise DockerError("Docker host must be an explicit local Unix socket")
    path = value[len("unix://"):]
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "\x00" in path
        or "\\" in path
        or posixpath.normpath(path) != path
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise DockerError("Docker host must be a normalized absolute Unix socket path")
    return value


@dataclass(frozen=True)
class SandboxReady:
    input_tree: str
    executable: dict
    runner_sha256: str


@dataclass(frozen=True)
class SandboxResult:
    status: str
    failure: str
    exit_code: object
    started_at: object
    finished_at: object
    output_tree: object
    stdout: bytes
    stderr: bytes
    workspace: bytes


class DockerRunner:
    """Prepare a pinned container, then wait for capability authorization."""

    def __init__(
        self,
        image,
        platform,
        docker_host,
        deployment_id,
        runtime_domain_id,
        docker="docker",
        handshake_seconds=30,
    ):
        if _PROCESS_POISON_REASON is not None:
            raise DockerError("gateway process is poisoned by ambiguous teardown")
        executable = shutil.which(docker)
        if executable is None:
            raise DockerError("docker is not available")
        self.docker = str(Path(executable).resolve())
        self._docker_client = str(
            Path(__file__).with_name("docker_client.py").resolve()
        )
        self._launcher = [
            sys.executable,
            "-I",
            "-S",
            self._docker_client,
            self.docker,
        ]
        self.image = image
        self.platform = platform
        self.docker_host = _docker_host(docker_host)
        self.deployment_id = deployment_id
        self.runtime_domain_id = runtime_domain_id
        self.container_name = runtime_container_name(runtime_domain_id)
        self.deployment_sha256 = _identity_sha256(
            deployment_id, "deployment identity"
        )
        self.runtime_domain_sha256 = self.container_name.removeprefix(
            "underwrite-"
        )
        self.handshake_seconds = handshake_seconds
        self._poisoned = None
        self._docker_config = tempfile.TemporaryDirectory(
            prefix="underwrite-docker-config-"
        )
        self.environment = {
            "DOCKER_CONFIG": self._docker_config.name,
            "DOCKER_HOST": self.docker_host,
            "HOME": self._docker_config.name,
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": os.path.dirname(self.docker),
        }
        try:
            self._inspect_daemon_security()
            self._inspect_image()
        except Exception:
            self._docker_config.cleanup()
            raise

    def close(self):
        self._docker_config.cleanup()

    @property
    def poisoned(self):
        return self._poisoned is not None or _PROCESS_POISON_REASON is not None

    def _remove_container(self, name):
        global _PROCESS_POISON_REASON
        try:
            self._command(["rm", "--force", name], check=False)
            remaining = self._command(
                [
                    "container",
                    "ls",
                    "--all",
                    "--quiet",
                    "--filter",
                    f"name=^/{name}$",
                ],
                check=False,
            )
            if remaining.returncode or remaining.stdout.strip():
                raise DockerError("container remains after forced removal")
        except Exception as error:
            self._poisoned = "container teardown could not be verified"
            _PROCESS_POISON_REASON = self._poisoned
            raise DockerError(self._poisoned) from error

    def reconcile(self):
        if self.poisoned:
            raise DockerError("gateway process is poisoned by ambiguous teardown")
        self._remove_container(self.container_name)

    def _command(self, arguments, data=None, check=True):
        try:
            completed = subprocess.run(
                [*self._launcher, *arguments],
                input=data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.environment,
                timeout=_DOCKER_COMMAND_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise DockerError("docker command timed out") from error
        except OSError as error:
            raise DockerError("docker could not be started") from error
        if check and completed.returncode:
            detail = completed.stderr.decode("utf-8", "replace").strip()
            raise DockerError(f"docker {' '.join(arguments[:2])} failed: {detail}")
        return completed

    def _inspect_image(self):
        completed = self._command(["image", "inspect", self.image])
        try:
            values = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise DockerError("docker returned malformed image metadata") from error
        if not isinstance(values, list) or len(values) != 1:
            raise DockerError("docker did not resolve exactly one runtime image")
        image = values[0]
        architecture = self.platform.split("/", 1)[1]
        if (
            not isinstance(image, dict)
            or image.get("Id") != self.image
            or image.get("Os") != "linux"
            or image.get("Architecture") != architecture
        ):
            raise DockerError("runtime image identity or platform does not match policy")

    def _inspect_daemon_security(self):
        completed = self._command(
            ["info", "--format", "{{json .SecurityOptions}}"]
        )
        try:
            options = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise DockerError("docker returned malformed security metadata") from error
        if (
            not isinstance(options, list)
            or not all(isinstance(value, str) for value in options)
            or not any(value.startswith("name=seccomp,") for value in options)
        ):
            raise DockerError("docker daemon does not report an active seccomp profile")

    def prepare(self, request, workspace):
        if self.poisoned:
            raise DockerError("Docker runner is poisoned by ambiguous teardown")
        if not isinstance(request, dict):
            raise DockerError("sandbox request must be an object")
        if not isinstance(workspace, bytes):
            raise DockerError("sandbox workspace must be bytes")
        limits = request.get("sandbox", {}).get("limits", {})
        try:
            memory = int(limits["memoryBytes"])
            workspace_limit = int(limits["workspaceBytes"])
        except (KeyError, TypeError, ValueError) as error:
            raise DockerError("sandbox limits are missing") from error
        if limits.get("processes") != 1:
            raise DockerError("Docker v1 runner requires a one-process job")
        if memory < 64 * 1024 * 1024:
            raise DockerError("Docker runner requires at least 64 MiB of memory")
        name = self.container_name
        arguments = [
            "create",
            "--name",
            name,
            "--pull",
            "never",
            "--platform",
            self.platform,
            "--network",
            "none",
            "--ipc",
            "none",
            "--cgroupns",
            "private",
            "--read-only",
            "--no-healthcheck",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "DAC_READ_SEARCH",
            "--cap-add",
            "KILL",
            "--cap-add",
            "SETGID",
            "--cap-add",
            "SETUID",
            "--security-opt",
            "no-new-privileges=true",
            "--security-opt",
            "seccomp=builtin",
            "--pids-limit",
            "2",
            "--memory",
            str(memory),
            "--memory-swap",
            str(memory),
            "--memory-swappiness",
            "0",
            "--oom-score-adj",
            "500",
            "--log-driver",
            "none",
            "--tmpfs",
            "/workspace:rw,nosuid,nodev,noexec,size="
            + str(workspace_limit)
            + ",mode=0700",
            "--tmpfs",
            "/run/underwrite:rw,nosuid,nodev,noexec,size=1048576,mode=0700",
            "--shm-size",
            "4096",
            "--ulimit",
            "nofile=256:256",
            "--stop-timeout",
            "1",
            "--workdir",
            "/",
            "--user",
            "0:0",
            "--env",
            "UNDERWRITE_RUNNER=1",
            "--label",
            "org.underwrite.gateway=v1",
            "--label",
            "org.underwrite.deployment-sha256=" + self.deployment_sha256,
            "--label",
            "org.underwrite.runtime-domain-sha256=" + self.runtime_domain_sha256,
            "--interactive",
            "--entrypoint",
            "/usr/local/bin/python3",
            self.image,
            "-I",
            "-S",
            "/opt/underwrite/sandbox_runner.py",
        ]
        handle = None
        try:
            self._command(arguments)
            metadata = self._container_metadata(name)
            self._verify_container(metadata, request)
            diagnostics = tempfile.TemporaryFile()
            try:
                process = subprocess.Popen(
                    [
                        *self._launcher,
                        "start",
                        "--attach",
                        "--interactive",
                        name,
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=diagnostics,
                    env=self.environment,
                )
            except OSError as error:
                diagnostics.close()
                raise DockerError("docker could not attach the runner") from error
            handle = PreparedDockerRun(
                owner=self,
                name=name,
                process=process,
                diagnostics=diagnostics,
                limits=limits,
            )
            try:
                handle.initialize(request, workspace, self.handshake_seconds)
            except Exception as error:
                detail = handle._diagnostic_text()
                handle.close()
                if detail:
                    raise DockerError(
                        f"runner readiness failed: {detail}"
                    ) from error
                raise
            return handle
        except Exception:
            if handle is None:
                self._remove_container(name)
            raise

    def _container_metadata(self, name):
        completed = self._command(["container", "inspect", name])
        try:
            values = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise DockerError("docker returned malformed container metadata") from error
        if not isinstance(values, list) or len(values) != 1:
            raise DockerError("docker did not resolve exactly one runner container")
        return values[0]

    def _verify_container(self, metadata, request):
        host = metadata.get("HostConfig")
        config = metadata.get("Config")
        if not isinstance(host, dict) or not isinstance(config, dict):
            raise DockerError("runner container metadata is incomplete")
        limits = request["sandbox"]["limits"]
        checks = (
            (metadata.get("Name") == "/" + self.container_name, "container name"),
            (metadata.get("Image") == self.image, "image"),
            (host.get("NetworkMode") == "none", "network namespace"),
            (host.get("IpcMode") == "none", "IPC namespace"),
            (host.get("PidMode") == "", "PID namespace"),
            (host.get("UTSMode") == "", "UTS namespace"),
            (host.get("CgroupnsMode") == "private", "cgroup namespace"),
            (host.get("ReadonlyRootfs") is True, "read-only root"),
            (host.get("Privileged") is False, "unprivileged mode"),
            (host.get("AutoRemove") is False, "inspection retention"),
            (host.get("PidsLimit") == 2, "PID limit"),
            (host.get("Memory") == limits["memoryBytes"], "memory limit"),
            (host.get("MemorySwap") == limits["memoryBytes"], "swap limit"),
            (
                host.get("LogConfig") == {"Type": "none", "Config": {}},
                "disabled container logging",
            ),
            (
                host.get("RestartPolicy")
                == {"Name": "no", "MaximumRetryCount": 0},
                "restart policy",
            ),
            (not host.get("Binds"), "host bind absence"),
            (not host.get("Devices"), "host device absence"),
            (not config.get("Volumes"), "image volume absence"),
            (config.get("OpenStdin") is True, "gateway control input"),
            (config.get("AttachStdin") is True, "attached gateway control input"),
            (config.get("Entrypoint") == ["/usr/local/bin/python3"], "runner entrypoint"),
            (
                config.get("Cmd")
                == ["-I", "-S", "/opt/underwrite/sandbox_runner.py"],
                "runner command",
            ),
            (
                config.get("Healthcheck") == {"Test": ["NONE"]},
                "disabled image healthcheck",
            ),
            (
                config.get("Labels")
                == {
                    "org.underwrite.deployment-sha256": self.deployment_sha256,
                    "org.underwrite.gateway": "v1",
                    "org.underwrite.runtime-domain-sha256": self.runtime_domain_sha256,
                },
                "deployment labels",
            ),
        )
        for valid, label in checks:
            if not valid:
                raise DockerError(f"runner container failed {label} preflight")
        security = host.get("SecurityOpt")
        accepted_security = {
            frozenset(("no-new-privileges=true", "seccomp=builtin")),
            frozenset(("no-new-privileges:true", "seccomp=builtin")),
        }
        if (
            not isinstance(security, list)
            or not all(isinstance(value, str) for value in security)
            or len(security) != 2
            or frozenset(security) not in accepted_security
        ):
            raise DockerError("runner container has unexpected security options")
        if {value.upper() for value in (host.get("CapDrop") or [])} != {"ALL"}:
            raise DockerError("runner container did not drop every ambient capability")
        added = {
            value.upper().removeprefix("CAP_")
            for value in (host.get("CapAdd") or [])
        }
        if added != {
            "CHOWN",
            "DAC_READ_SEARCH",
            "KILL",
            "SETGID",
            "SETUID",
        }:
            raise DockerError("runner container has unexpected added capabilities")
        mounts = metadata.get("Mounts") or []
        if mounts:
            raise DockerError("runner container has an unexpected persistent mount")
        tmpfs = host.get("Tmpfs") or {}
        if set(tmpfs) != {"/workspace", "/run/underwrite"}:
            raise DockerError("runner container has unexpected tmpfs mounts")
        expected_tmpfs = {
            "/workspace": {
                "rw",
                "nosuid",
                "nodev",
                "noexec",
                f"size={limits['workspaceBytes']}",
                "mode=0700",
            },
            "/run/underwrite": {
                "rw",
                "nosuid",
                "nodev",
                "noexec",
                "size=1048576",
                "mode=0700",
            },
        }
        for path, expected in expected_tmpfs.items():
            if set(tmpfs[path].split(",")) != expected:
                raise DockerError(
                    f"runner container has weakened tmpfs options for {path}"
                )


class PreparedDockerRun:
    def __init__(self, owner, name, process, diagnostics, limits):
        self.owner = owner
        self.name = name
        self.process = process
        self.diagnostics = diagnostics
        self.limits = limits
        self.ready = None
        self.finished = False
        self.removed = False

    def initialize(self, request, workspace, timeout):
        request_bytes = json.dumps(
            request,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(request_bytes) > _MAX_CONTROL_BYTES:
            raise DockerError("sandbox request is too large")
        _write_frame(self.process.stdin, request_bytes)
        _write_frame(self.process.stdin, workspace)
        deadline = time.monotonic() + timeout
        value = _json(
            _read_frame(self.process.stdout, _MAX_CONTROL_BYTES, deadline),
            "runner readiness",
        )
        if set(value) != {"status", "inputTree", "executable", "runnerSha256"}:
            raise DockerError("runner readiness has the wrong fields")
        if value["status"] != "ready":
            raise DockerError("runner did not become ready")
        for name in ("inputTree", "runnerSha256"):
            if not isinstance(value[name], str) or not _SHA256.fullmatch(value[name]):
                raise DockerError(f"runner readiness {name} is invalid")
        if not isinstance(value["executable"], dict):
            raise DockerError("runner executable descriptor is invalid")
        self.ready = SandboxReady(
            input_tree=value["inputTree"],
            executable=value["executable"],
            runner_sha256=value["runnerSha256"],
        )

    def run(self, capability_payload_sha256, issued_at, expires_at):
        if self.finished:
            raise DockerError("prepared runner is already terminal")
        if not isinstance(capability_payload_sha256, str) or not _SHA256.fullmatch(
            capability_payload_sha256
        ):
            raise DockerError("capability payload digest is invalid")
        issued_text = _timestamp_text(issued_at, "capability issuedAt")
        expires_text = _timestamp_text(expires_at, "capability expiresAt")
        issued = _timestamp(issued_text, "capability issuedAt")
        expires = _timestamp(expires_text, "capability expiresAt")
        if expires <= issued or (expires - issued).total_seconds() > 300:
            raise DockerError("capability lifetime is invalid")
        command = json.dumps(
            {
                "command": "start",
                "capabilityPayloadSha256": capability_payload_sha256,
                "issuedAt": issued_text,
                "expiresAt": expires_text,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        _write_frame(self.process.stdin, command)
        deadline = time.monotonic() + self.limits["wallSeconds"] + 30
        value = _json(
            _read_frame(self.process.stdout, _MAX_CONTROL_BYTES, deadline),
            "runner result",
        )
        required = {
            "status",
            "failure",
            "exitCode",
            "startedAt",
            "finishedAt",
            "outputTree",
        }
        if set(value) != required:
            raise DockerError("runner result has the wrong fields")
        status = value["status"]
        if status not in ("exited", "failed"):
            raise DockerError("runner result status is invalid")
        if not isinstance(value["failure"], str):
            raise DockerError("runner failure detail is invalid")
        if status == "failed":
            if (
                not value["failure"]
                or value["exitCode"] is not None
                or value["startedAt"] is not None
                or value["finishedAt"] is not None
                or value["outputTree"] is not None
            ):
                raise DockerError(
                    "runner failed result contains invalid evidence or failure"
                )
            result = SandboxResult(
                status=status,
                failure=value["failure"],
                exit_code=None,
                started_at=None,
                finished_at=None,
                output_tree=None,
                stdout=b"",
                stderr=b"",
                workspace=b"",
            )
        else:
            if value["failure"]:
                raise DockerError("runner exited result contains a failure detail")
            exit_code = value["exitCode"]
            if isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code < 0:
                raise DockerError("runner exit code is invalid")
            output_tree = value["outputTree"]
            if not isinstance(output_tree, str) or not _SHA256.fullmatch(output_tree):
                raise DockerError("runner output tree is invalid")
            stdout = _read_frame(
                self.process.stdout, self.limits["outputBytes"], deadline
            )
            stderr = _read_frame(
                self.process.stdout, self.limits["outputBytes"], deadline
            )
            if len(stdout) + len(stderr) > self.limits["outputBytes"]:
                raise DockerError("runner streams exceed the combined output limit")
            workspace = _read_frame(
                self.process.stdout,
                self.limits["workspaceBytes"] + _MAX_CONTROL_BYTES,
                deadline,
            )
            started_at = _timestamp(value["startedAt"], "startedAt")
            finished_at = _timestamp(value["finishedAt"], "finishedAt")
            if finished_at < started_at:
                raise DockerError("runner finishedAt is earlier than startedAt")
            result = SandboxResult(
                status=status,
                failure="",
                exit_code=exit_code,
                started_at=started_at,
                finished_at=finished_at,
                output_tree=output_tree,
                stdout=stdout,
                stderr=stderr,
                workspace=workspace,
            )
        try:
            self.process.stdin.close()
        except OSError:
            pass
        remaining = max(1, deadline - time.monotonic())
        try:
            return_code = self.process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            self.cancel()
            raise DockerError("runner did not terminate after returning evidence") from error
        if return_code != 0:
            raise DockerError("runner container process returned a failure")
        metadata = self.owner._container_metadata(self.name)
        state = metadata.get("State") or {}
        if (
            state.get("Running") is not False
            or state.get("Pid") != 0
            or state.get("OOMKilled") is not False
            or state.get("ExitCode") != 0
        ):
            raise DockerError("runner teardown did not reach a clean stopped state")
        self.finished = True
        return result

    def _diagnostic_text(self):
        self.diagnostics.seek(0)
        return self.diagnostics.read(8192).decode("utf-8", "replace").strip()

    def cancel(self):
        if self.finished:
            return
        if self.process.poll() is None:
            try:
                _write_frame(
                    self.process.stdin,
                    b'{"command":"cancel"}',
                )
                self.process.wait(timeout=2)
            except (DockerError, OSError, ValueError, subprocess.TimeoutExpired):
                pass
        self.owner._remove_container(self.name)
        self.removed = True
        if self.process.poll() is None:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.finished = True

    def close(self):
        try:
            self.cancel()
            if not self.removed:
                self.owner._remove_container(self.name)
                self.removed = True
        finally:
            for stream in (self.process.stdin, self.process.stdout):
                try:
                    stream.close()
                except (AttributeError, OSError):
                    pass
            self.diagnostics.close()
