#!/usr/bin/env python3
"""Host-side contracts for the Docker execution transport."""

import copy
import io
import json
import os
import struct
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from gateway import docker_client, docker_runner


IMAGE = "sha256:" + "a" * 64
PLATFORM = "linux/arm64"
DOCKER_HOST = "unix:///var/run/docker.sock"
DEPLOYMENT_ID = "urn:underwrite:gateway:test:revision-7"
RUNTIME_DOMAIN_ID = "urn:underwrite:runtime-domain:test:docker-a"
CONTAINER_NAME = docker_runner.runtime_container_name(RUNTIME_DOMAIN_ID)
DOCKER_LAUNCHER = [
    "/usr/bin/python3",
    "-I",
    "-S",
    "/gateway/docker_client.py",
    "/usr/bin/docker",
]
INPUT_TREE = "b" * 64
OUTPUT_TREE = "c" * 64
RUNNER_SHA256 = "d" * 64
CAPABILITY_SHA256 = "e" * 64
ISSUED_AT = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
EXPIRES_AT = ISSUED_AT + timedelta(minutes=5)
EXECUTABLE = {
    "path": "/usr/local/bin/python3",
    "sha256": "f" * 64,
    "bytes": 6_000_000,
}
LIMITS = {
    "wallSeconds": 10,
    "cpuSeconds": 5,
    "memoryBytes": 128 * 1024 * 1024,
    "processes": 1,
    "workspaceBytes": 64 * 1024 * 1024,
    "outputBytes": 10,
}
REQUEST = {
    "version": 1,
    "job": {
        "argv": ["/usr/local/bin/python3", "-c", "print('ok')"],
        "cwd": "source",
        "environment": {"LANG": "C"},
        "executable": EXECUTABLE,
        "stdin": "closed",
    },
    "sandbox": {"limits": LIMITS},
    "inputTree": INPUT_TREE,
    "targetUid": 65532,
    "targetGid": 65532,
    "runnerSha256": RUNNER_SHA256,
}


def frame(data):
    return struct.pack(">Q", len(data)) + data


def json_frame(value):
    return frame(
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )


def clean_result(**updates):
    value = {
        "status": "exited",
        "failure": "",
        "exitCode": 0,
        "startedAt": "2026-08-29T12:00:00Z",
        "finishedAt": "2026-08-29T12:00:01Z",
        "outputTree": OUTPUT_TREE,
    }
    value.update(updates)
    return value


def container_metadata():
    return {
        "Name": "/" + CONTAINER_NAME,
        "Image": IMAGE,
        "State": {
            "Running": False,
            "Pid": 0,
            "OOMKilled": False,
            "ExitCode": 0,
        },
        "HostConfig": {
            "NetworkMode": "none",
            "IpcMode": "none",
            "PidMode": "",
            "UTSMode": "",
            "CgroupnsMode": "private",
            "ReadonlyRootfs": True,
            "Privileged": False,
            "AutoRemove": False,
            "PidsLimit": 2,
            "Memory": LIMITS["memoryBytes"],
            "MemorySwap": LIMITS["memoryBytes"],
            "LogConfig": {"Type": "none", "Config": {}},
            "Binds": None,
            "Devices": None,
            "SecurityOpt": ["no-new-privileges=true", "seccomp=builtin"],
            "CapDrop": ["ALL"],
            "CapAdd": [
                "CHOWN",
                "DAC_READ_SEARCH",
                "KILL",
                "SETGID",
                "SETUID",
            ],
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "Tmpfs": {
                "/workspace": (
                    "rw,nosuid,nodev,noexec,size=67108864,mode=0700"
                ),
                "/run/underwrite": (
                    "rw,nosuid,nodev,noexec,size=1048576,mode=0700"
                ),
            },
        },
        "Config": {
            "Volumes": None,
            "OpenStdin": True,
            "AttachStdin": True,
            "Entrypoint": ["/usr/local/bin/python3"],
            "Cmd": ["-I", "-S", "/opt/underwrite/sandbox_runner.py"],
            "Healthcheck": {"Test": ["NONE"]},
            "Labels": {
                "org.underwrite.deployment-sha256": docker_runner._identity_sha256(
                    DEPLOYMENT_ID, "deployment identity"
                ),
                "org.underwrite.gateway": "v1",
                "org.underwrite.runtime-domain-sha256": CONTAINER_NAME.removeprefix(
                    "underwrite-"
                ),
            },
        },
        "Mounts": [],
    }


class FakeProcess:
    def __init__(self, output=b"", wait_results=(0,), poll_result=0):
        self.stdin = io.BytesIO()
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, output)
        finally:
            os.close(write_fd)
        self.stdout = os.fdopen(read_fd, "rb", buffering=0)
        self.wait_results = list(wait_results)
        self.poll_result = poll_result
        self.kill_calls = 0

    def wait(self, timeout=None):
        if not self.wait_results:
            return 0
        value = self.wait_results.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def poll(self):
        return self.poll_result

    def kill(self):
        self.kill_calls += 1
        self.poll_result = -9


class FakeOwner:
    def __init__(self, metadata=None):
        self.metadata = metadata or container_metadata()
        self.commands = []

    def _container_metadata(self, name):
        return copy.deepcopy(self.metadata)

    def _command(self, arguments, data=None, check=True):
        self.commands.append((arguments, check))
        return subprocess.CompletedProcess(arguments, 0, b"", b"")

    def _remove_container(self, name):
        self._command(["rm", "--force", name], check=False)
        self._command(
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


class FramingTests(unittest.TestCase):
    def pipe_reader(self, data):
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, data)
        finally:
            os.close(write_fd)
        return os.fdopen(read_fd, "rb", buffering=0)

    def test_json_rejects_duplicate_fields(self):
        with self.assertRaisesRegex(docker_runner.DockerError, "duplicate runner field"):
            docker_runner._json(b'{"status":"ready","status":"failed"}', "reply")

    def test_read_frame_rejects_oversize_before_reading_body(self):
        stream = self.pipe_reader(struct.pack(">Q", 11))
        self.addCleanup(stream.close)
        with self.assertRaisesRegex(docker_runner.DockerError, "exceeds"):
            docker_runner._read_frame(stream, 10, time.monotonic() + 1)

    def test_read_frame_rejects_early_eof(self):
        stream = self.pipe_reader(struct.pack(">Q", 4) + b"abc")
        self.addCleanup(stream.close)
        with self.assertRaisesRegex(docker_runner.DockerError, "ended early"):
            docker_runner._read_frame(stream, 10, time.monotonic() + 1)

    def test_write_frame_normalizes_closed_channel_error(self):
        stream = io.BytesIO()
        stream.close()
        with self.assertRaises(docker_runner.DockerError):
            docker_runner._write_frame(stream, b"message")


class MetadataTests(unittest.TestCase):
    def runner_without_init(self):
        runner = object.__new__(docker_runner.DockerRunner)
        runner.docker = "/usr/bin/docker"
        runner.image = IMAGE
        runner.platform = PLATFORM
        runner.docker_host = DOCKER_HOST
        runner.deployment_id = DEPLOYMENT_ID
        runner.runtime_domain_id = RUNTIME_DOMAIN_ID
        runner.container_name = CONTAINER_NAME
        runner.deployment_sha256 = docker_runner._identity_sha256(
            DEPLOYMENT_ID, "deployment identity"
        )
        runner.runtime_domain_sha256 = CONTAINER_NAME.removeprefix("underwrite-")
        runner._launcher = list(DOCKER_LAUNCHER)
        runner._poisoned = None
        return runner

    def test_image_inspection_requires_exact_pinned_linux_platform(self):
        good = [{"Id": IMAGE, "Os": "linux", "Architecture": "arm64"}]
        completed = subprocess.CompletedProcess(
            ["docker"], 0, json.dumps(good).encode("utf-8"), b""
        )
        with mock.patch.object(
            docker_runner.shutil, "which", return_value="/usr/bin/docker"
        ), mock.patch.object(
            docker_runner.DockerRunner, "_inspect_daemon_security"
        ), mock.patch.object(
            docker_runner.DockerRunner, "_command", return_value=completed
        ):
            runner = docker_runner.DockerRunner(
                IMAGE,
                PLATFORM,
                DOCKER_HOST,
                DEPLOYMENT_ID,
                RUNTIME_DOMAIN_ID,
            )
        self.addCleanup(runner.close)
        self.assertEqual(runner.image, IMAGE)

        mutations = (
            ("image", [{"Id": "sha256:" + "0" * 64, "Os": "linux", "Architecture": "arm64"}]),
            ("operating system", [{"Id": IMAGE, "Os": "darwin", "Architecture": "arm64"}]),
            ("architecture", [{"Id": IMAGE, "Os": "linux", "Architecture": "amd64"}]),
            ("multiple images", good + good),
        )
        for label, metadata in mutations:
            response = subprocess.CompletedProcess(
                ["docker"], 0, json.dumps(metadata).encode("utf-8"), b""
            )
            with self.subTest(label=label), mock.patch.object(
                docker_runner.shutil, "which", return_value="/usr/bin/docker"
            ), mock.patch.object(
                docker_runner.DockerRunner, "_inspect_daemon_security"
            ), mock.patch.object(
                docker_runner.DockerRunner, "_command", return_value=response
            ):
                with self.assertRaises(docker_runner.DockerError):
                    docker_runner.DockerRunner(
                        IMAGE,
                        PLATFORM,
                        DOCKER_HOST,
                        DEPLOYMENT_ID,
                        RUNTIME_DOMAIN_ID,
                    )

    def test_runner_ignores_ambient_docker_client_configuration(self):
        ambient = {
            "DOCKER_CONFIG": "/tmp/attacker-config",
            "DOCKER_CONTEXT": "attacker-context",
            "DOCKER_HOST": "tcp://attacker.example:2375",
            "HOME": "/tmp/attacker-home",
        }
        with mock.patch.dict(os.environ, ambient), mock.patch.object(
            docker_runner.shutil,
            "which",
            return_value="/usr/bin/docker",
        ), mock.patch.object(
            docker_runner.DockerRunner,
            "_inspect_daemon_security",
        ), mock.patch.object(
            docker_runner.DockerRunner,
            "_inspect_image",
        ):
            runner = docker_runner.DockerRunner(
                IMAGE,
                PLATFORM,
                DOCKER_HOST,
                DEPLOYMENT_ID,
                RUNTIME_DOMAIN_ID,
            )
        self.addCleanup(runner.close)

        self.assertEqual(runner.environment["DOCKER_HOST"], DOCKER_HOST)
        self.assertNotIn("DOCKER_CONTEXT", runner.environment)
        self.assertNotEqual(
            runner.environment["DOCKER_CONFIG"],
            ambient["DOCKER_CONFIG"],
        )
        self.assertEqual(
            runner.environment["HOME"],
            runner.environment["DOCKER_CONFIG"],
        )

    def test_runner_requires_an_explicit_normalized_unix_docker_host(self):
        invalid = (
            "/var/run/docker.sock",
            "tcp://127.0.0.1:2375",
            "unix://relative.sock",
            "unix:////var/run/docker.sock",
            "unix:///var/../run/docker.sock",
        )
        with mock.patch.object(
            docker_runner.shutil,
            "which",
            return_value="/usr/bin/docker",
        ):
            for value in invalid:
                with self.subTest(value=value):
                    with self.assertRaises(docker_runner.DockerError):
                        docker_runner.DockerRunner(
                            IMAGE,
                            PLATFORM,
                            value,
                            DEPLOYMENT_ID,
                            RUNTIME_DOMAIN_ID,
                        )

    def test_runner_requires_explicit_deployment_and_runtime_domain_ids(self):
        with mock.patch.object(
            docker_runner.shutil,
            "which",
            return_value="/usr/bin/docker",
        ):
            for deployment_id, runtime_domain_id in (
                ("", RUNTIME_DOMAIN_ID),
                (DEPLOYMENT_ID, ""),
                (DEPLOYMENT_ID, "invalid\udcff"),
            ):
                with self.subTest(
                    deployment_id=deployment_id,
                    runtime_domain_id=runtime_domain_id,
                ):
                    with self.assertRaises(docker_runner.DockerError):
                        docker_runner.DockerRunner(
                            IMAGE,
                            PLATFORM,
                            DOCKER_HOST,
                            deployment_id,
                            runtime_domain_id,
                        )

    def test_reconcile_uses_the_stable_runtime_domain_container(self):
        runner = self.runner_without_init()
        runner._remove_container = mock.Mock()

        runner.reconcile()

        runner._remove_container.assert_called_once_with(CONTAINER_NAME)

    def test_daemon_must_report_an_active_seccomp_profile(self):
        runner = self.runner_without_init()
        good = subprocess.CompletedProcess(
            ["docker"],
            0,
            b'["name=seccomp,profile=builtin","name=cgroupns"]',
            b"",
        )
        runner._command = mock.Mock(return_value=good)
        runner._inspect_daemon_security()

        for value in (b"[]", b'["name=cgroupns"]', b"null", b"not-json"):
            runner._command = mock.Mock(
                return_value=subprocess.CompletedProcess(
                    ["docker"], 0, value, b""
                )
            )
            with self.subTest(value=value):
                with self.assertRaises(docker_runner.DockerError):
                    runner._inspect_daemon_security()

    def test_container_accepts_exact_isolation_metadata(self):
        runner = self.runner_without_init()
        runner._verify_container(container_metadata(), REQUEST)
        normalized = container_metadata()
        normalized["HostConfig"]["SecurityOpt"][0] = (
            "no-new-privileges:true"
        )
        runner._verify_container(normalized, REQUEST)

    def test_container_rejects_tampered_enforced_metadata(self):
        cases = (
            ("network", ("HostConfig", "NetworkMode"), "host"),
            ("memory", ("HostConfig", "Memory"), LIMITS["memoryBytes"] + 1),
            (
                "logging",
                ("HostConfig", "LogConfig"),
                {"Type": "json-file", "Config": {}},
            ),
            ("bind", ("HostConfig", "Binds"), ["/host:/workspace"]),
            ("capability", ("HostConfig", "CapAdd"), ["KILL", "SYS_ADMIN"]),
            ("entrypoint", ("Config", "Entrypoint"), ["/bin/sh"]),
            ("healthcheck", ("Config", "Healthcheck"), None),
            ("deployment label", ("Config", "Labels"), {}),
        )
        runner = self.runner_without_init()
        for label, path, value in cases:
            metadata = container_metadata()
            metadata[path[0]][path[1]] = value
            with self.subTest(label=label):
                with self.assertRaises(docker_runner.DockerError):
                    runner._verify_container(metadata, REQUEST)

    def test_container_rejects_host_namespaces(self):
        runner = self.runner_without_init()
        for field in ("PidMode", "UTSMode", "CgroupnsMode"):
            metadata = container_metadata()
            metadata["HostConfig"][field] = "host"
            with self.subTest(field=field):
                with self.assertRaises(docker_runner.DockerError):
                    runner._verify_container(metadata, REQUEST)

    def test_container_requires_the_pinned_builtin_seccomp_profile(self):
        for security in (
            ["no-new-privileges=true"],
            ["no-new-privileges=true", "seccomp=unconfined"],
            [
                "no-new-privileges=true",
                "seccomp=builtin",
                "seccomp=unconfined",
            ],
        ):
            metadata = container_metadata()
            metadata["HostConfig"]["SecurityOpt"] = security
            with self.subTest(security=security):
                with self.assertRaises(docker_runner.DockerError):
                    self.runner_without_init()._verify_container(metadata, REQUEST)

    def test_container_requires_exact_no_new_privileges_metadata(self):
        runner = self.runner_without_init()
        for security in (
            ["no-new-privileges=false", "seccomp=builtin"],
            ["no-new-privileges", "seccomp=builtin"],
            [
                "no-new-privileges=true",
                "no-new-privileges=false",
                "seccomp=builtin",
            ],
        ):
            metadata = container_metadata()
            metadata["HostConfig"]["SecurityOpt"] = security
            with self.subTest(security=security):
                with self.assertRaises(docker_runner.DockerError):
                    runner._verify_container(metadata, REQUEST)

    def test_docker_commands_have_a_fixed_timeout(self):
        runner = self.runner_without_init()
        runner.docker = "/usr/bin/docker"
        runner.environment = {}
        timeout = subprocess.TimeoutExpired([runner.docker, "info"], 30)
        with mock.patch.object(
            docker_runner.subprocess,
            "run",
            side_effect=timeout,
        ) as run:
            with self.assertRaisesRegex(docker_runner.DockerError, "timed out"):
                runner._command(["info"])
        self.assertEqual(run.call_args.kwargs["timeout"], 30)
        self.assertEqual(
            run.call_args.args[0],
            [*runner._launcher, "info"],
        )

    def test_container_rejects_weakened_tmpfs_options(self):
        metadata = container_metadata()
        metadata["HostConfig"]["Tmpfs"]["/workspace"] = (
            "rw,nosuid,nodev,size=67108864,mode=0700"
        )
        with self.assertRaises(docker_runner.DockerError):
            self.runner_without_init()._verify_container(metadata, REQUEST)


class DockerClientTests(unittest.TestCase):
    def test_wrapper_restores_only_the_inherited_soft_limit_before_exec(self):
        arguments = ["/usr/bin/docker", "info"]
        with mock.patch.object(
            docker_client.sys,
            "argv",
            ["docker_client.py", *arguments],
        ), mock.patch.object(
            docker_client.resource,
            "getrlimit",
            return_value=(123, 456),
        ), mock.patch.object(
            docker_client.resource,
            "setrlimit",
        ) as setrlimit, mock.patch.object(
            docker_client.os,
            "execve",
            side_effect=OSError,
        ) as execve:
            self.assertEqual(docker_client.main(), 70)

        setrlimit.assert_called_once_with(
            docker_client.resource.RLIMIT_AS,
            (456, 456),
        )
        self.assertEqual(execve.call_args.args[:2], (arguments[0], arguments))
        self.assertIs(execve.call_args.args[2], docker_client.os.environ)

    def test_wrapper_rejects_a_nonabsolute_docker_path(self):
        with mock.patch.object(
            docker_client.sys,
            "argv",
            ["docker_client.py", "docker", "info"],
        ), mock.patch.object(
            docker_client.resource,
            "setrlimit",
        ) as setrlimit:
            self.assertEqual(docker_client.main(), 64)
        setrlimit.assert_not_called()


class ResultTests(unittest.TestCase):
    def handle(self, result, stdout=b"", stderr=b"", workspace=b"archive"):
        output = b"".join(
            (json_frame(result), frame(stdout), frame(stderr), frame(workspace))
        )
        process = FakeProcess(output)
        diagnostics = tempfile.TemporaryFile()
        handle = docker_runner.PreparedDockerRun(
            FakeOwner(), "underwrite-test", process, diagnostics, dict(LIMITS)
        )
        self.addCleanup(handle.close)
        return handle

    def run_handle(self, handle):
        return handle.run(CAPABILITY_SHA256, ISSUED_AT, EXPIRES_AT)

    def test_clean_result_returns_only_validated_evidence(self):
        handle = self.handle(clean_result(), b"out", b"err", b"workspace")
        result = self.run_handle(handle)
        self.assertEqual(result.status, "exited")
        self.assertEqual(result.stdout, b"out")
        self.assertEqual(result.stderr, b"err")
        self.assertEqual(result.workspace, b"workspace")
        self.assertEqual(result.started_at.tzinfo, timezone.utc)
        self.assertLess(result.started_at, result.finished_at)

    def test_combined_stream_limit_is_enforced(self):
        handle = self.handle(clean_result(), b"123456", b"12345")
        with self.assertRaisesRegex(docker_runner.DockerError, "combined output"):
            self.run_handle(handle)

    def test_result_rejects_noncanonical_timestamp(self):
        handle = self.handle(clean_result(startedAt="2026-08-29Z"))
        with self.assertRaisesRegex(docker_runner.DockerError, "timestamp"):
            self.run_handle(handle)

    def test_result_rejects_finished_before_started(self):
        handle = self.handle(
            clean_result(
                startedAt="2026-08-29T12:00:02Z",
                finishedAt="2026-08-29T12:00:01Z",
            )
        )
        with self.assertRaisesRegex(docker_runner.DockerError, "finished"):
            self.run_handle(handle)

    def test_exited_result_rejects_failure_detail(self):
        handle = self.handle(clean_result(failure="policy bypassed"))
        with self.assertRaisesRegex(docker_runner.DockerError, "failure"):
            self.run_handle(handle)

    def test_failed_result_does_not_accept_execution_artifacts(self):
        result = clean_result(
            status="failed",
            failure="target setup failed",
            exitCode=0,
            startedAt="2026-08-29T12:00:00Z",
            finishedAt="2026-08-29T12:00:01Z",
            outputTree=OUTPUT_TREE,
        )
        handle = self.handle(result)
        with self.assertRaisesRegex(docker_runner.DockerError, "failed result"):
            self.run_handle(handle)

    def test_failed_result_returns_no_execution_evidence(self):
        result = clean_result(
            status="failed",
            failure="target setup failed",
            exitCode=None,
            startedAt=None,
            finishedAt=None,
            outputTree=None,
        )
        handle = self.handle(result)
        value = self.run_handle(handle)
        self.assertEqual(value.status, "failed")
        self.assertEqual(value.failure, "target setup failed")
        self.assertIsNone(value.exit_code)
        self.assertEqual(value.stdout, b"")
        self.assertEqual(value.workspace, b"")

    def test_failed_result_requires_failure_detail(self):
        result = clean_result(
            status="failed",
            failure="",
            exitCode=None,
            startedAt=None,
            finishedAt=None,
            outputTree=None,
        )
        handle = self.handle(result)
        with self.assertRaisesRegex(docker_runner.DockerError, "failure"):
            self.run_handle(handle)

    def test_capability_lifetime_is_validated_before_start(self):
        handle = self.handle(clean_result())
        with self.assertRaisesRegex(docker_runner.DockerError, "lifetime"):
            handle.run(
                CAPABILITY_SHA256,
                ISSUED_AT,
                ISSUED_AT + timedelta(seconds=301),
            )
        self.assertEqual(handle.process.stdin.getvalue(), b"")


class CleanupTests(unittest.TestCase):
    def test_prepare_removes_container_when_metadata_preflight_fails(self):
        runner = object.__new__(docker_runner.DockerRunner)
        runner.docker = "/usr/bin/docker"
        runner.image = IMAGE
        runner.platform = PLATFORM
        runner.environment = {}
        runner.handshake_seconds = 1
        runner._launcher = list(DOCKER_LAUNCHER)
        runner._poisoned = None
        runner.container_name = CONTAINER_NAME
        runner.deployment_sha256 = docker_runner._identity_sha256(
            DEPLOYMENT_ID, "deployment identity"
        )
        runner.runtime_domain_sha256 = CONTAINER_NAME.removeprefix("underwrite-")
        runner._command = mock.Mock(
            return_value=subprocess.CompletedProcess([], 0, b"", b"")
        )
        metadata = container_metadata()
        metadata["HostConfig"]["NetworkMode"] = "host"
        runner._container_metadata = mock.Mock(return_value=metadata)
        with self.assertRaises(docker_runner.DockerError):
            runner.prepare(REQUEST, b"workspace")
        create = runner._command.mock_calls[0].args[0]
        self.assertIn("--no-healthcheck", create)
        self.assertEqual(create[create.index("--log-driver") + 1], "none")
        self.assertIn(
            mock.call(["rm", "--force", CONTAINER_NAME], check=False),
            runner._command.mock_calls,
        )

    def test_close_removes_container_and_closes_channels(self):
        process = FakeProcess(poll_result=0)
        diagnostics = tempfile.TemporaryFile()
        owner = FakeOwner()
        handle = docker_runner.PreparedDockerRun(
            owner, "underwrite-test", process, diagnostics, dict(LIMITS)
        )
        handle.close()
        self.assertTrue(handle.finished)
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(diagnostics.closed)
        self.assertIn((["rm", "--force", "underwrite-test"], False), owner.commands)

    def test_prepare_closes_attach_process_when_handshake_fails(self):
        runner = object.__new__(docker_runner.DockerRunner)
        runner.docker = "/usr/bin/docker"
        runner.image = IMAGE
        runner.platform = PLATFORM
        runner.environment = {}
        runner.handshake_seconds = 1
        runner._launcher = list(DOCKER_LAUNCHER)
        runner._poisoned = None
        runner.container_name = CONTAINER_NAME
        runner.deployment_sha256 = docker_runner._identity_sha256(
            DEPLOYMENT_ID, "deployment identity"
        )
        runner.runtime_domain_sha256 = CONTAINER_NAME.removeprefix("underwrite-")
        runner._command = mock.Mock(
            return_value=subprocess.CompletedProcess([], 0, b"", b"")
        )
        runner._container_metadata = mock.Mock(return_value=container_metadata())
        readiness = {
            "status": "failed",
            "inputTree": INPUT_TREE,
            "executable": EXECUTABLE,
            "runnerSha256": RUNNER_SHA256,
        }
        process = FakeProcess(json_frame(readiness), poll_result=None)
        diagnostics = tempfile.TemporaryFile()
        self.addCleanup(lambda: not process.stdin.closed and process.stdin.close())
        self.addCleanup(lambda: not process.stdout.closed and process.stdout.close())
        self.addCleanup(lambda: not diagnostics.closed and diagnostics.close())
        with mock.patch.object(
            docker_runner.subprocess, "Popen", return_value=process
        ) as popen, mock.patch.object(
            docker_runner.tempfile, "TemporaryFile", return_value=diagnostics
        ):
            with self.assertRaisesRegex(docker_runner.DockerError, "become ready"):
                runner.prepare(REQUEST, b"workspace")
        self.assertEqual(
            popen.call_args.args[0],
            [
                *runner._launcher,
                "start",
                "--attach",
                "--interactive",
                CONTAINER_NAME,
            ],
        )
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(diagnostics.closed)
        self.assertIn(
            mock.call(["rm", "--force", CONTAINER_NAME], check=False),
            runner._command.mock_calls,
        )

    def test_cancel_removes_runner_when_control_input_is_closed(self):
        process = FakeProcess(poll_result=None)
        process.stdin.close()
        owner = FakeOwner()
        diagnostics = tempfile.TemporaryFile()
        self.addCleanup(diagnostics.close)
        handle = docker_runner.PreparedDockerRun(
            owner, "underwrite-test", process, diagnostics, dict(LIMITS)
        )
        self.addCleanup(process.stdout.close)
        handle.cancel()
        self.assertTrue(handle.finished)
        self.assertTrue(handle.removed)
        self.assertIn((["rm", "--force", "underwrite-test"], False), owner.commands)
        self.assertIn(
            (
                [
                    "container",
                    "ls",
                    "--all",
                    "--quiet",
                    "--filter",
                    "name=^/underwrite-test$",
                ],
                False,
            ),
            owner.commands,
        )

    def test_ambiguous_removal_poisons_the_runner_and_closes_channels(self):
        with mock.patch.object(docker_runner, "_PROCESS_POISON_REASON", None):
            owner = object.__new__(docker_runner.DockerRunner)
            owner._poisoned = None
            owner._command = mock.Mock(
                side_effect=(
                    subprocess.CompletedProcess(
                        ["docker", "rm"], 1, b"", b"failed"
                    ),
                    subprocess.CompletedProcess(
                        ["docker", "container", "ls"],
                        0,
                        b"container-id\n",
                        b"",
                    ),
                )
            )
            process = FakeProcess(poll_result=0)
            diagnostics = tempfile.TemporaryFile()
            handle = docker_runner.PreparedDockerRun(
                owner, "underwrite-test", process, diagnostics, dict(LIMITS)
            )

            with self.assertRaisesRegex(docker_runner.DockerError, "teardown"):
                handle.close()

            replacement = object.__new__(docker_runner.DockerRunner)
            replacement._poisoned = None
            self.assertTrue(owner.poisoned)
            self.assertTrue(replacement.poisoned)
            self.assertTrue(process.stdin.closed)
            self.assertTrue(process.stdout.closed)
            self.assertTrue(diagnostics.closed)

    def test_removal_command_failure_poisons_the_gateway_process(self):
        with mock.patch.object(docker_runner, "_PROCESS_POISON_REASON", None):
            owner = object.__new__(docker_runner.DockerRunner)
            owner._poisoned = None
            owner._command = mock.Mock(
                side_effect=docker_runner.DockerError("docker command timed out")
            )

            with self.assertRaisesRegex(docker_runner.DockerError, "teardown"):
                owner._remove_container("underwrite-test")

            replacement = object.__new__(docker_runner.DockerRunner)
            replacement._poisoned = None
            self.assertTrue(owner.poisoned)
            self.assertTrue(replacement.poisoned)


if __name__ == "__main__":
    unittest.main()
