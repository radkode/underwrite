#!/usr/bin/env python3
"""Host execution broker orchestration and fail-closed boundary tests."""

import base64
import concurrent.futures
import copy
import fcntl
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gateway import artifacts
from gateway import broker as broker_module
from gateway.broker import (
    ExecutionBroker,
    ExecutionFailed,
    ExecutionRequest,
    GatewayError,
    GatewayPolicy,
)
from gateway.store import ReplayConflict, StoredExecution


SIGNER_ID = "https://runner.example/hosts/runner-7"
SESSION_ID = "71f9b79c-9b2a-4b89-a69c-f3ed3a032abc"
CHALLENGE = "7" * 64
RUNNER_SHA256 = "1" * 64
EXECUTABLE_SHA256 = "2" * 64
SOURCE_BYTES = b"frozen source bundle\n"
NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
DEPLOYMENT_ID = "urn:underwrite:gateway:test:dd-2174"
RUNTIME_DOMAIN_ID = "urn:underwrite:runtime-domain:test:docker-a"
DOCKER_HOST = "unix:///var/run/docker.sock"


def sandbox_policy():
    return {
        "policy": (
            "https://github.com/radkode/underwrite/sandbox-policy/v1"
        ),
        "credentials": "absent",
        "network": "denied",
        "hostWrites": "denied",
        "gitHooks": "disabled",
        "gitFilters": "disabled",
        "timeout": "enforced",
        "limits": {
            "wallSeconds": 60,
            "cpuSeconds": 30,
            "memoryBytes": 134_217_728,
            "processes": 1,
            "workspaceBytes": 1_048_576,
            "outputBytes": 1_048_576,
        },
    }


def executable_policy():
    return {
        "path": "/usr/bin/python3",
        "sha256": EXECUTABLE_SHA256,
        "bytes": 1_048_576,
    }


def frozen_target(source=SOURCE_BYTES):
    return {
        "version": 1,
        "kind": "github_pr",
        "repo": "acme/widget",
        "number": 17,
        "state": "open",
        "merged_at": None,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "head_repo_id": 1234,
        "head_repo": "acme/widget-fork",
        "head_ref": "feature/execution",
        "merge_base_sha": "c" * 40,
        "changed_files": 3,
        "diff_sha256": "d" * 64,
        "diff_bytes": 2048,
        "trusted_context_sha256": "e" * 64,
        "trusted_context_bytes": 512,
        "object_bundle_sha256": hashlib.sha256(source).hexdigest(),
        "object_bundle_bytes": len(source),
    }


def policy_value(**updates):
    value = {
        "signer_id": SIGNER_ID,
        "deployment_id": DEPLOYMENT_ID,
        "runtime_domain_id": RUNTIME_DOMAIN_ID,
        "docker_host": DOCKER_HOST,
        "image": "sha256:" + "0" * 64,
        "platform": "linux/arm64",
        "runner_sha256": RUNNER_SHA256,
        "executable": executable_policy(),
        "environment": {"LANG": "C.UTF-8", "TZ": "UTC"},
        "sandbox": sandbox_policy(),
        "target_uid": 65532,
        "target_gid": 65532,
    }
    value.update(updates)
    return value


def request_value(policy, **updates):
    value = {
        "version": 1,
        "sessionId": SESSION_ID,
        "challenge": CHALLENGE,
        "target": frozen_target(),
        "action": {"seq": 41, "beat": 7, "attempt": 2},
        "job": {
            "argv": [policy.executable["path"], "-c", "print('ok')"],
            "cwd": ".",
            "environment": copy.deepcopy(policy.environment),
            "executable": copy.deepcopy(policy.executable),
            "stdin": "closed",
        },
        "sandbox": copy.deepcopy(policy.sandbox),
        "exitCode": 0,
    }
    value.update(copy.deepcopy(updates))
    return value


def payload_from_envelope(envelope):
    value = json.loads(envelope.decode("utf-8"))
    return base64.b64decode(value["payload"], validate=True)


class FakeSigner:
    key_id = "sha256:" + "f" * 64
    algorithm = "ecdsa-p256-sha256"

    def __init__(self, signer_id=SIGNER_ID):
        self.signer_id = signer_id
        self.sign_calls = []
        self.verify_calls = []
        self._lock = threading.Lock()

    @staticmethod
    def _signature(pae):
        return hashlib.sha256(b"test signing key\0" + pae).digest()

    def sign(self, pae, key_id):
        if key_id != self.key_id:
            raise AssertionError("unexpected signing key ID")
        with self._lock:
            self.sign_calls.append((pae, key_id))
        return self._signature(pae)

    def verify(self, pae, key_id, signature):
        if key_id != self.key_id or signature != self._signature(pae):
            raise ValueError("test signature does not verify")
        with self._lock:
            self.verify_calls.append((pae, key_id, signature))
        return self.signer_id


class FakePreparedRun:
    def __init__(self, owner, request, result):
        self.owner = owner
        self.ready = SimpleNamespace(
            input_tree=request["inputTree"],
            executable=copy.deepcopy(request["job"]["executable"]),
            runner_sha256=request["runnerSha256"],
        )
        self.result = result
        self.run_digests = []
        self.capability_windows = []
        self.cancel_calls = 0
        self.close_calls = 0

    def run(self, capability_payload_sha256, issued_at, expires_at):
        with self.owner.lock:
            self.owner.run_calls += 1
        if self.owner.run_started is not None:
            self.owner.run_started.set()
        self.run_digests.append(capability_payload_sha256)
        self.capability_windows.append((issued_at, expires_at))
        if self.owner.run_gate is not None:
            if not self.owner.run_gate.wait(10):
                raise RuntimeError("test run gate timed out")
        if self.owner.run_error is not None:
            raise self.owner.run_error
        return self.result

    def cancel(self):
        self.cancel_calls += 1

    def close(self):
        self.close_calls += 1
        if self.owner.close_error is not None:
            raise self.owner.close_error


class FakeRunner:
    def __init__(
        self,
        result,
        image="sha256:" + "0" * 64,
        platform="linux/arm64",
        docker_host=DOCKER_HOST,
        deployment_id=DEPLOYMENT_ID,
        runtime_domain_id=RUNTIME_DOMAIN_ID,
    ):
        self.result = result
        self.image = image
        self.platform = platform
        self.docker_host = docker_host
        self.deployment_id = deployment_id
        self.runtime_domain_id = runtime_domain_id
        self.poisoned = False
        self.reconcile_calls = 0
        self.reconcile_error = None
        self.prepared = []
        self.prepare_calls = []
        self.prepare_barrier = None
        self.run_gate = None
        self.run_started = None
        self.run_error = None
        self.close_error = None
        self.run_calls = 0
        self.lock = threading.Lock()

    def reconcile(self):
        with self.lock:
            self.reconcile_calls += 1
        if self.reconcile_error is not None:
            raise self.reconcile_error

    def prepare(self, request, workspace):
        if self.prepare_barrier is not None:
            self.prepare_barrier.wait(timeout=10)
        prepared = FakePreparedRun(self, request, self.result)
        with self.lock:
            self.prepare_calls.append((copy.deepcopy(request), workspace))
            self.prepared.append(prepared)
        return prepared


class BrokerCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.source_path = self.base / "source.bundle"
        self.source_path.write_bytes(SOURCE_BYTES)
        self.policy = GatewayPolicy(**policy_value())
        self.request = request_value(self.policy)
        output = self.base / "runner-output"
        output.mkdir()
        (output / "result.txt").write_bytes(b"verified output\n")
        limit = self.policy.sandbox["limits"]["workspaceBytes"]
        output_measurement = artifacts.synthetic_git_tree(
            output,
            maximum_bytes=limit,
        )
        output_archive = artifacts.pack_workspace(
            output,
            maximum_bytes=limit,
        )
        self.result = SimpleNamespace(
            status="exited",
            failure="",
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
            output_tree=output_measurement,
            stdout=b"job stdout\n",
            stderr=b"job stderr\n",
            workspace=output_archive,
        )
        self.signer = FakeSigner()
        self.runner = FakeRunner(self.result)
        self.source_calls = []
        self.source_patch = mock.patch.object(
            broker_module.artifacts,
            "verify_source_bundle",
            side_effect=self.verify_source_bundle,
        )
        self.verify_source = self.source_patch.start()

    def tearDown(self):
        self.source_patch.stop()
        self.temporary.cleanup()

    def verify_source_bundle(
        self,
        bundle,
        target,
        workspace,
        *,
        maximum_workspace_bytes,
    ):
        self.source_calls.append(
            (bundle, copy.deepcopy(target), Path(workspace), maximum_workspace_bytes)
        )
        if bundle != SOURCE_BYTES:
            raise artifacts.ArtifactError(
                "source bundle bytes do not match the frozen target"
            )
        if target != self.request["target"]:
            raise artifacts.ArtifactError("source target is not authoritative")
        workspace = Path(workspace)
        workspace.mkdir()
        (workspace / "source.py").write_bytes(b"print('source')\n")
        tree = artifacts.synthetic_git_tree(
            workspace,
            maximum_bytes=maximum_workspace_bytes,
        )
        descriptor = artifacts.ArtifactDescriptor(
            tree,
            hashlib.sha256(bundle).hexdigest(),
            len(bundle),
        )
        return artifacts.VerifiedSource(descriptor, workspace)

    def make_broker(self, *, runner=None, signer=None, store="store"):
        return ExecutionBroker(
            self.policy,
            signer or self.signer,
            self.base / store,
            runner or self.runner,
            clock=lambda: NOW,
            dedicated_process=True,
        )

    def replay_key(self, broker, request=None):
        parsed = ExecutionRequest(request or self.request, self.policy)
        return broker._identity(parsed)[0]

    def row(self, broker, request=None):
        key = self.replay_key(broker, request)
        with sqlite3.connect(str(broker.ledger.path)) as database:
            database.row_factory = sqlite3.Row
            return database.execute(
                "SELECT * FROM attempts WHERE replay_key = ?",
                (key,),
            ).fetchone()


class GatewayPolicyTests(unittest.TestCase):
    def test_policy_accepts_only_the_fixed_v1_isolation_profile(self):
        policy = GatewayPolicy(**policy_value())
        self.assertEqual(policy.executor_manifest["backend"], "docker-single-process-v1")
        self.assertEqual(
            policy.executor_manifest["runtimeDomainId"], RUNTIME_DOMAIN_ID
        )
        self.assertEqual(policy.executor_manifest["sandbox"], sandbox_policy())
        self.assertEqual(
            policy.executor_manifest["hostResources"],
            {
                "dedicatedProcess": True,
                "hostPlatform": policy.host_platform,
                "addressSpace": {
                    "enforcement": "soft-rlimit",
                    "mode": (
                        "additional-to-measured-baseline"
                        if policy.host_platform.startswith("darwin/")
                        else "absolute"
                    ),
                    "bytes": 768 * 1024 * 1024,
                    "trustedChildren": {
                        "dockerClient": {
                            "mode": "restore-soft-to-inherited-hard",
                        },
                        "git": {
                            "enforcement": "hard-rlimit",
                            "mode": (
                                "additional-to-measured-baseline"
                                if policy.host_platform.startswith("darwin/")
                                else "absolute"
                            ),
                            "bytes": 2 * 1024 * 1024 * 1024,
                        },
                    },
                },
                "concurrentExecutions": 1,
                "concurrencyScope": "runtime-domain-store-and-container-name",
                "containerName": "underwrite-"
                + hashlib.sha256(RUNTIME_DOMAIN_ID.encode("utf-8")).hexdigest(),
                "queueSeconds": 30,
                "sourceBundleBytes": 64 * 1024 * 1024,
                "workspaceBytes": 64 * 1024 * 1024,
                "capturedOutputBytes": 16 * 1024 * 1024,
            },
        )
        self.assertEqual(
            policy.executor_manifest["signingAlgorithm"],
            "ecdsa-p256-sha256",
        )
        self.assertRegex(
            policy.executor_id,
            r"^urn:underwrite:executor:docker-single-process-v1:[0-9a-f]{64}$",
        )

        invalid = (
            ("mutable image tag", {"image": "python:3.13"}),
            ("unsupported platform", {"platform": "linux/riscv64"}),
            ("root target", {"target_uid": 0}),
            ("missing runtime domain", {"runtime_domain_id": ""}),
            ("boolean capability lifetime", {"capability_seconds": True}),
            (
                "multiple target processes",
                {
                    "sandbox": {
                        **sandbox_policy(),
                        "limits": {
                            **sandbox_policy()["limits"],
                            "processes": 2,
                        },
                    }
                },
            ),
            (
                "credential environment",
                {"environment": {"LANG": "C.UTF-8", "GITHUB_TOKEN": "value"}},
            ),
            (
                "unrecognized credential environment",
                {"environment": {"LANG": "C.UTF-8", "API_KEY": "value"}},
            ),
            (
                "unsafe value in an allowed environment field",
                {"environment": {"LANG": "en_US.UTF-8"}},
            ),
            (
                "excessive workspace",
                {
                    "sandbox": {
                        **sandbox_policy(),
                        "limits": {
                            **sandbox_policy()["limits"],
                            "workspaceBytes": 64 * 1024 * 1024 + 1,
                        },
                    }
                },
            ),
            (
                "excessive captured output",
                {
                    "sandbox": {
                        **sandbox_policy(),
                        "limits": {
                            **sandbox_policy()["limits"],
                            "outputBytes": 16 * 1024 * 1024 + 1,
                        },
                    }
                },
            ),
            (
                "excessive target memory",
                {
                    "sandbox": {
                        **sandbox_policy(),
                        "limits": {
                            **sandbox_policy()["limits"],
                            "memoryBytes": 2 * 1024 * 1024 * 1024 + 1,
                        },
                    }
                },
            ),
            (
                "CPU time above wall time",
                {
                    "sandbox": {
                        **sandbox_policy(),
                        "limits": {
                            **sandbox_policy()["limits"],
                            "cpuSeconds": 61,
                        },
                    }
                },
            ),
            ("excessive source bundle", {"max_source_bundle_bytes": 64 * 1024 * 1024 + 1}),
        )
        for label, update in invalid:
            with self.subTest(label=label):
                with self.assertRaises(GatewayError):
                    GatewayPolicy(**policy_value(**update))

    def test_policy_rejects_noncanonical_executable_paths_and_unsafe_integers(self):
        invalid_paths = (
            "usr/bin/python3",
            "//usr/bin/python3",
            "/usr//bin/python3",
            "/usr/../bin/python3",
            "/usr/bin/./python3",
            "/usr\\bin\\python3",
            "/usr/bin/python3\x01",
        )
        for path in invalid_paths:
            with self.subTest(path=path):
                executable = executable_policy()
                executable["path"] = path
                with self.assertRaises(GatewayError):
                    GatewayPolicy(**policy_value(executable=executable))

        sandbox = sandbox_policy()
        sandbox["limits"]["wallSeconds"] = 9_007_199_254_740_992
        with self.assertRaises(GatewayError):
            GatewayPolicy(**policy_value(sandbox=sandbox))

    def test_policy_nested_values_are_immutable_and_manifest_is_defensive(self):
        values = policy_value()
        policy = GatewayPolicy(**values)
        executor_id = policy.executor_id
        values["environment"]["TOKEN"] = "later mutation"
        values["sandbox"]["limits"]["processes"] = 99
        values["executable"]["path"] = "/tmp/other"

        self.assertEqual(policy.environment, {"LANG": "C.UTF-8", "TZ": "UTC"})
        self.assertEqual(policy.sandbox["limits"]["processes"], 1)
        self.assertEqual(policy.executable["path"], "/usr/bin/python3")
        self.assertEqual(policy.executor_id, executor_id)

        with self.assertRaises(TypeError):
            policy.environment["TOKEN"] = "direct mutation"
        with self.assertRaises(TypeError):
            policy.sandbox["limits"]["processes"] = 99
        with self.assertRaises(TypeError):
            policy.executable["path"] = "/tmp/other"
        with self.assertRaises(TypeError):
            policy.environment |= {"TOKEN": "union mutation"}
        with self.assertRaises(TypeError):
            policy.sandbox["limits"] |= {"processes": 99}

        manifest = policy.executor_manifest
        manifest["environment"]["TOKEN"] = "manifest mutation"
        manifest["sandbox"]["limits"]["processes"] = 99
        manifest["executable"]["path"] = "/tmp/other"
        self.assertEqual(policy.environment, {"LANG": "C.UTF-8", "TZ": "UTC"})
        self.assertEqual(policy.sandbox["limits"]["processes"], 1)
        self.assertEqual(policy.executable["path"], "/usr/bin/python3")
        self.assertEqual(policy.executor_id, executor_id)

    def test_host_memory_budget_preserves_the_inherited_hard_limit(self):
        with mock.patch.object(
            broker_module.sys,
            "platform",
            "linux",
        ), mock.patch.object(
            broker_module,
            "_linux_address_space_bytes",
            return_value=128 * 1024 * 1024,
        ), mock.patch.object(
            broker_module.resource,
            "getrlimit",
            return_value=(512 * 1024 * 1024, broker_module.resource.RLIM_INFINITY),
        ), mock.patch.object(
            broker_module.resource,
            "setrlimit",
        ) as setrlimit:
            broker_module._enforce_host_memory_budget()
        setrlimit.assert_called_once_with(
            broker_module.resource.RLIMIT_AS,
            (768 * 1024 * 1024, broker_module.resource.RLIM_INFINITY),
        )

    def test_linux_memory_budget_rejects_an_oversized_baseline(self):
        with mock.patch.object(
            broker_module.sys,
            "platform",
            "linux",
        ), mock.patch.object(
            broker_module,
            "_linux_address_space_bytes",
            return_value=768 * 1024 * 1024,
        ):
            with self.assertRaisesRegex(GatewayError, "baseline"):
                broker_module._enforce_host_memory_budget()

    def test_broker_requires_an_explicit_dedicated_process_claim(self):
        policy = GatewayPolicy(**policy_value())
        with self.assertRaisesRegex(GatewayError, "dedicated-process"):
            ExecutionBroker(
                policy,
                FakeSigner(),
                Path("/tmp/not-used"),
                FakeRunner(SimpleNamespace()),
                dedicated_process=False,
            )


class ExecutionRequestTests(unittest.TestCase):
    def setUp(self):
        self.policy = GatewayPolicy(**policy_value())
        self.request = request_value(self.policy)

    def test_request_is_exact_and_bound_to_trusted_job_and_sandbox(self):
        parsed = ExecutionRequest(self.request, self.policy)
        self.assertEqual(json.loads(parsed.bytes), self.request)

        invalid = []
        unknown = copy.deepcopy(self.request)
        unknown["extra"] = True
        invalid.append(unknown)
        wrong_environment = copy.deepcopy(self.request)
        wrong_environment["job"]["environment"]["EXTRA"] = "untrusted"
        invalid.append(wrong_environment)
        wrong_executable = copy.deepcopy(self.request)
        wrong_executable["job"]["argv"][0] = "/bin/sh"
        invalid.append(wrong_executable)
        wrong_sandbox = copy.deepcopy(self.request)
        wrong_sandbox["sandbox"]["network"] = "allowed"
        invalid.append(wrong_sandbox)
        open_stdin = copy.deepcopy(self.request)
        open_stdin["job"]["stdin"] = "inherit"
        invalid.append(open_stdin)

        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(GatewayError):
                    ExecutionRequest(value, self.policy)

    def test_request_rejects_noncanonical_paths_and_unsafe_integers(self):
        invalid = []
        for cwd in ("", "/tmp", "source//tests", "source/../tests", "source\\tests"):
            value = copy.deepcopy(self.request)
            value["job"]["cwd"] = cwd
            invalid.append(value)
        for field in ("seq", "beat", "attempt"):
            value = copy.deepcopy(self.request)
            value["action"][field] = 9_007_199_254_740_992
            invalid.append(value)
        value = copy.deepcopy(self.request)
        value["exitCode"] = 9_007_199_254_740_992
        invalid.append(value)

        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(GatewayError):
                    ExecutionRequest(value, self.policy)

    def test_request_rejects_an_invalid_frozen_target_before_host_work(self):
        value = copy.deepcopy(self.request)
        value["target"]["head_sha"] = "NOT-A-COMMIT"
        with self.assertRaises(GatewayError):
            ExecutionRequest(value, self.policy)


class ExecutionBrokerTests(BrokerCase):
    def test_poisoned_runner_refuses_future_gateway_work(self):
        broker = self.make_broker()
        self.runner.poisoned = True

        with self.assertRaisesRegex(GatewayError, "poisoned"):
            broker.execute(self.request, self.source_path)

        self.assertEqual(self.source_calls, [])
        self.assertEqual(self.runner.prepare_calls, [])

    def test_runner_image_and_platform_are_bound_to_the_signed_policy(self):
        for label, runner in (
            (
                "image",
                FakeRunner(self.result, image="sha256:" + "9" * 64),
            ),
            (
                "platform",
                FakeRunner(self.result, platform="linux/amd64"),
            ),
            (
                "Docker host",
                FakeRunner(
                    self.result,
                    docker_host="unix:///var/run/other-docker.sock",
                ),
            ),
            (
                "runtime domain",
                FakeRunner(
                    self.result,
                    runtime_domain_id="urn:underwrite:runtime-domain:other",
                ),
            ),
        ):
            with self.subTest(label=label):
                with self.assertRaisesRegex(GatewayError, "must match gateway policy"):
                    self.make_broker(runner=runner)

    def test_reconciliation_failure_stops_before_source_or_replay_work(self):
        broker = self.make_broker()
        self.runner.reconcile_error = RuntimeError("orphan state is ambiguous")

        with self.assertRaisesRegex(GatewayError, "reconcile"):
            broker.execute(self.request, self.source_path)

        self.assertEqual(self.runner.reconcile_calls, 1)
        self.assertEqual(self.source_calls, [])
        self.assertEqual(self.runner.prepare_calls, [])

    def test_reconciliation_failure_cannot_return_completed_replay(self):
        broker = self.make_broker()
        stored = broker.execute(self.request, self.source_path)
        self.runner.reconcile_error = RuntimeError("orphan state is ambiguous")

        with self.assertRaisesRegex(GatewayError, "reconcile"):
            broker.execute(self.request, self.source_path)

        self.assertIsInstance(stored, StoredExecution)
        self.assertEqual(self.runner.reconcile_calls, 2)
        self.assertEqual(len(self.source_calls), 1)
        self.assertEqual(len(self.runner.prepare_calls), 1)

    def test_reconciliation_and_execution_run_inside_cross_process_store_lease(self):
        broker = self.make_broker()
        events = []
        original_reconcile = self.runner.reconcile
        original_execute = broker._execute
        lock_probe = (
            "import fcntl,os,sys;"
            "descriptor=os.open(sys.argv[1],os.O_RDWR);"
            "status=1;"
            "\ntry: fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)"
            "\nexcept BlockingIOError: status=0"
            "\nos.close(descriptor);raise SystemExit(status)"
        )

        def reconcile():
            completed = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    lock_probe,
                    str(broker.content.root / "execution.lock"),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            events.append(("reconcile", completed.returncode))
            original_reconcile()

        def execute(request, source):
            events.append(("execute", None))
            return original_execute(request, source)

        self.runner.reconcile = reconcile
        with mock.patch.object(broker, "_execute", side_effect=execute):
            broker.execute(self.request, self.source_path)

        self.assertEqual(events, [("reconcile", 0), ("execute", None)])

    def test_store_lease_is_private_and_released_after_execution(self):
        broker = self.make_broker()
        broker.execute(self.request, self.source_path)
        lock_path = broker.content.root / "execution.lock"

        self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    @unittest.skipUnless(hasattr(os, "fork"), "fork is required")
    def test_store_lease_serializes_separate_gateway_processes(self):
        broker = self.make_broker()
        first = broker._acquire_store_lease()
        read_descriptor, write_descriptor = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(read_descriptor)
            os.close(first)
            broker_module._HOST_EXECUTION_WAIT_SECONDS = 0.1
            try:
                second = broker._acquire_store_lease()
            except GatewayError:
                os.write(write_descriptor, b"blocked")
                os._exit(0)
            else:
                fcntl.flock(second, fcntl.LOCK_UN)
                os.close(second)
                os.write(write_descriptor, b"acquired")
                os._exit(1)

        os.close(write_descriptor)
        try:
            outcome = os.read(read_descriptor, 16)
            _pid, status = os.waitpid(child, 0)
        finally:
            os.close(read_descriptor)
            fcntl.flock(first, fcntl.LOCK_UN)
            os.close(first)
        self.assertEqual(outcome, b"blocked")
        self.assertTrue(os.WIFEXITED(status))
        self.assertEqual(os.WEXITSTATUS(status), 0)

    def test_success_persists_verified_evidence_from_exact_bytes(self):
        broker = self.make_broker()

        stored = broker.execute(self.request, self.source_path)

        self.assertIsInstance(stored, StoredExecution)
        self.assertEqual(len(self.runner.prepare_calls), 1)
        self.assertEqual(self.runner.run_calls, 1)
        self.assertEqual(len(self.signer.sign_calls), 2)
        self.assertEqual(len(self.signer.verify_calls), 2)
        self.assertEqual(len(self.source_calls), 1)
        bundle, target, _workspace, workspace_limit = self.source_calls[0]
        self.assertEqual(bundle, SOURCE_BYTES)
        self.assertEqual(target, self.request["target"])
        self.assertEqual(
            workspace_limit,
            self.policy.sandbox["limits"]["workspaceBytes"],
        )

        prepared = self.runner.prepared[0]
        capability_payload = payload_from_envelope(stored.capability)
        self.assertEqual(
            prepared.run_digests,
            [hashlib.sha256(capability_payload).hexdigest()],
        )
        self.assertEqual(
            prepared.capability_windows,
            [(NOW, NOW.replace(minute=5))],
        )
        self.assertEqual(prepared.cancel_calls, 0)
        self.assertEqual(prepared.close_calls, 1)

        source = stored.artifact("sourceBundle")
        output = stored.artifact("outputBundle")
        stdout = stored.artifact("stdout")
        stderr = stored.artifact("stderr")
        self.assertEqual(broker.content.read(source), SOURCE_BYTES)
        self.assertEqual(broker.content.read(stdout), self.result.stdout)
        self.assertEqual(broker.content.read(stderr), self.result.stderr)
        output_bytes = broker.content.read(output)
        output_descriptor = artifacts.verify_output_bundle(
            output_bytes,
            maximum_workspace_bytes=self.policy.sandbox["limits"]["workspaceBytes"],
            maximum_bundle_bytes=self.policy.sandbox["limits"]["workspaceBytes"] * 2,
        )
        self.assertEqual(output_descriptor.git_tree, self.result.output_tree)

        receipt = json.loads(payload_from_envelope(stored.receipt))
        streams = receipt["predicate"]["streams"]
        self.assertEqual(
            streams["stdout"],
            {
                "sha256": hashlib.sha256(self.result.stdout).hexdigest(),
                "bytes": len(self.result.stdout),
                "truncated": False,
            },
        )
        self.assertEqual(
            streams["stderr"],
            {
                "sha256": hashlib.sha256(self.result.stderr).hexdigest(),
                "bytes": len(self.result.stderr),
                "truncated": False,
            },
        )
        validation = json.loads(stored.validation)
        self.assertEqual(validation["status"], "accepted")
        self.assertEqual(validation["outputTree"], self.result.output_tree)

    def test_failed_runner_never_signs_or_persists_a_receipt(self):
        self.result.status = "failed"
        self.result.failure = "resource limit exceeded"
        self.result.exit_code = None
        broker = self.make_broker()

        with self.assertRaisesRegex(ExecutionFailed, "resource limit"):
            broker.execute(self.request, self.source_path)

        self.assertEqual(len(self.signer.sign_calls), 1)
        self.assertEqual(len(self.signer.verify_calls), 0)
        row = self.row(broker)
        self.assertEqual(row["state"], "failed")
        self.assertIsNotNone(row["capability"])
        self.assertIsNone(row["receipt"])
        self.assertEqual(self.runner.prepared[0].cancel_calls, 0)
        self.assertEqual(self.runner.prepared[0].close_calls, 1)

    def test_teardown_failure_never_signs_or_persists_a_receipt(self):
        broker = self.make_broker()
        self.runner.close_error = RuntimeError("container absence is ambiguous")

        with self.assertRaisesRegex(GatewayError, "failed closed"):
            broker.execute(self.request, self.source_path)

        self.assertEqual(len(self.signer.sign_calls), 1)
        self.assertEqual(len(self.signer.verify_calls), 0)
        row = self.row(broker)
        self.assertEqual(row["state"], "failed")
        self.assertIsNone(row["receipt"])
        self.assertEqual(self.runner.prepared[0].close_calls, 1)

    def test_exit_tree_and_stream_mismatches_fail_before_receipt_signing(self):
        cases = (
            ("exit code", {"exit_code": 7}),
            ("output tree", {"output_tree": "9" * 64}),
            (
                "stream limit",
                {
                    "stdout": b"x"
                    * (self.policy.sandbox["limits"]["outputBytes"] + 1),
                    "stderr": b"",
                },
            ),
        )
        for index, (label, changes) in enumerate(cases):
            with self.subTest(label=label):
                result = copy.copy(self.result)
                for name, value in changes.items():
                    setattr(result, name, value)
                signer = FakeSigner()
                runner = FakeRunner(result)
                broker = self.make_broker(
                    runner=runner,
                    signer=signer,
                    store=f"failure-{index}",
                )

                with self.assertRaises(ExecutionFailed):
                    broker.execute(self.request, self.source_path)

                self.assertEqual(len(signer.sign_calls), 1)
                self.assertEqual(len(signer.verify_calls), 0)
                row = self.row(broker)
                self.assertEqual(row["state"], "failed")
                self.assertIsNone(row["receipt"])

    def test_output_bundle_tree_must_equal_the_measured_workspace(self):
        broker = self.make_broker()
        other_tree = "9" * 64
        bundle = b"mismatched output bundle"
        descriptor = artifacts.ArtifactDescriptor(
            other_tree,
            hashlib.sha256(bundle).hexdigest(),
            len(bundle),
        )
        built = artifacts.BuiltOutputBundle(bundle, descriptor)

        with mock.patch.object(
            broker_module.artifacts,
            "build_output_bundle",
            return_value=built,
        ), mock.patch.object(
            broker_module.artifacts,
            "verify_output_bundle",
            return_value=descriptor,
        ):
            with self.assertRaises(ExecutionFailed):
                broker.execute(self.request, self.source_path)

        self.assertEqual(len(self.signer.sign_calls), 1)
        self.assertEqual(len(self.signer.verify_calls), 0)
        self.assertIsNone(self.row(broker)["receipt"])

    def test_source_rejection_happens_before_runner_or_signer_use(self):
        broker = self.make_broker()
        self.verify_source.side_effect = artifacts.ArtifactError(
            "source does not match frozen target"
        )

        with self.assertRaises((artifacts.ArtifactError, GatewayError)):
            broker.execute(self.request, self.source_path)

        self.assertEqual(self.runner.prepare_calls, [])
        self.assertEqual(self.signer.sign_calls, [])
        row = self.row(broker)
        self.assertEqual(row["state"], "failed")
        self.assertIn("source does not match", row["failure"])
        self.assertEqual(list(broker.content.objects.rglob("*")), [])

    def test_exact_completed_replay_does_no_new_host_work(self):
        broker = self.make_broker()
        first = broker.execute(self.request, self.source_path)
        source_calls = len(self.source_calls)
        prepare_calls = len(self.runner.prepare_calls)
        sign_calls = len(self.signer.sign_calls)

        replay = broker.execute(copy.deepcopy(self.request), self.source_path)

        self.assertEqual(replay, first)
        self.assertEqual(len(self.source_calls), source_calls)
        self.assertEqual(len(self.runner.prepare_calls), prepare_calls)
        self.assertEqual(len(self.signer.sign_calls), sign_calls)
        self.assertEqual(self.runner.reconcile_calls, 2)

    def test_completed_replay_rejects_different_source_artifact(self):
        broker = self.make_broker()
        broker.execute(self.request, self.source_path)
        self.source_path.write_bytes(b"different source artifact\n")

        with self.assertRaises(ReplayConflict):
            broker.execute(copy.deepcopy(self.request), self.source_path)

        self.assertEqual(len(self.runner.prepare_calls), 1)
        self.assertEqual(len(self.signer.sign_calls), 2)

    def test_concurrent_replay_has_one_execution_owner(self):
        runner = FakeRunner(self.result)
        run_gate = threading.Event()
        run_started = threading.Event()
        runner.run_gate = run_gate
        runner.run_started = run_started
        first = self.make_broker(runner=runner, store="race-store")
        second = self.make_broker(runner=runner, store="race-store")

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first_result = pool.submit(
                first.execute,
                copy.deepcopy(self.request),
                self.source_path,
            )
            self.assertTrue(run_started.wait(10))
            second_result = pool.submit(
                second.execute,
                copy.deepcopy(self.request),
                self.source_path,
            )
            run_gate.set()
            futures = [first_result, second_result]
            outcomes = [future.result(timeout=20) for future in futures]

        successes = [value for value in outcomes if isinstance(value, StoredExecution)]
        self.assertEqual(len(successes), 2)
        self.assertEqual(successes[0], successes[1])
        self.assertEqual(runner.run_calls, 1)
        self.assertEqual(len(self.source_calls), 1)
        row = self.row(first)
        self.assertEqual(row["state"], "complete")
        self.assertIsNotNone(row["receipt"])

    def test_reusing_a_challenge_for_another_action_is_a_conflict(self):
        broker = self.make_broker()
        broker.execute(self.request, self.source_path)
        changed = copy.deepcopy(self.request)
        changed["action"]["seq"] += 1

        with self.assertRaises(ReplayConflict):
            broker.execute(changed, self.source_path)

        self.assertEqual(self.runner.run_calls, 1)
        self.assertEqual(len(self.signer.sign_calls), 2)
        self.assertEqual(len(self.runner.prepared), 1)

    def test_reusing_a_session_challenge_after_executor_rotation_is_a_conflict(self):
        first = self.make_broker(store="rotated-store")
        first.execute(self.request, self.source_path)
        rotated_policy = GatewayPolicy(
            **policy_value(image="sha256:" + "8" * 64)
        )
        rotated_runner = FakeRunner(
            self.result,
            image=rotated_policy.image,
            platform=rotated_policy.platform,
        )
        second = ExecutionBroker(
            rotated_policy,
            self.signer,
            self.base / "rotated-store",
            rotated_runner,
            clock=lambda: NOW,
            dedicated_process=True,
        )

        with self.assertRaises(ReplayConflict):
            second.execute(copy.deepcopy(self.request), self.source_path)

        self.assertEqual(rotated_runner.run_calls, 0)


if __name__ == "__main__":
    unittest.main()
