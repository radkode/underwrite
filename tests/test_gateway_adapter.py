#!/usr/bin/env python3
"""Operator adapter configuration, publication, and process-boundary tests."""

import copy
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gateway import adapter
from gateway.store import ContentStore, StoredExecution


EVIDENCE_FILES = {
    "request.json",
    "capability.dsse.json",
    "receipt.dsse.json",
    "output.bundle",
    "stdout",
    "stderr",
}


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class GatewayAdapterCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="underwrite-adapter-",
            dir="/tmp",
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        os.chown(self.root, -1, os.getegid())
        self.root.chmod(0o710)
        self.private_key = self.root / "private.pem"
        self.public_key = self.root / "public.pem"
        self.private_key.write_bytes(b"test private key\n")
        self.public_key.write_bytes(b"test public key\n")
        self.private_key.chmod(0o600)
        self.public_key.chmod(0o600)
        self.store_root = self.root / "store"
        self.store_root.mkdir(mode=0o700)
        self.content = ContentStore(self.store_root)
        self.source_bundle = self.root / "source.bundle"
        self.source_bytes = b"frozen source bundle\n"
        self.source_bundle.write_bytes(self.source_bytes)
        self.source_bundle.chmod(0o600)
        self.profile = self.profile_value()
        self.configuration = self.configuration_value()
        self.request = self.request_value()
        self.request_path = self.root / "request.json"
        self.request_path.write_bytes(canonical(self.request))
        self.request_path.chmod(0o600)
        self.export_root = self.root / "exports"
        self.export_root.mkdir(mode=0o710)
        self.evidence = self.export_root / "attempt-1"

    def profile_value(self):
        executable = {
            "path": "/usr/bin/python3",
            "sha256": "2" * 64,
            "bytes": 1_048_576,
        }
        sandbox = {
            "policy": "https://github.com/radkode/underwrite/sandbox-policy/v1",
            "credentials": "absent",
            "network": "denied",
            "hostWrites": "denied",
            "gitHooks": "disabled",
            "gitFilters": "disabled",
            "timeout": "enforced",
            "limits": {
                "wallSeconds": 60,
                "cpuSeconds": 30,
                "memoryBytes": 128 * 1024 * 1024,
                "processes": 1,
                "workspaceBytes": 4 * 1024 * 1024,
                "outputBytes": 1024 * 1024,
            },
        }
        return {
            "version": 1,
            "keyId": "sha256:" + "f" * 64,
            "signerId": "urn:underwrite:signer:test",
            "executorId": "urn:underwrite:executor:test",
            "job": {
                "argv": [executable["path"], "-I", "implement.py"],
                "cwd": ".",
                "environment": {"LANG": "C.UTF-8", "TZ": "UTC"},
                "executable": executable,
                "stdin": "closed",
            },
            "sandbox": sandbox,
            "exitCode": 0,
        }

    def configuration_value(self):
        executable = "/usr/bin/true"
        return {
            "version": 1,
            "privateKey": str(self.private_key),
            "publicKey": str(self.public_key),
            "storeRoot": str(self.store_root),
            "docker": executable,
            "openssl": executable,
            "git": executable,
            "deploymentId": "urn:underwrite:gateway:test:adapter",
            "runtimeDomainId": "urn:underwrite:runtime-domain:test:adapter",
            "dockerHost": "unix:///var/run/docker.sock",
            "image": "sha256:" + "0" * 64,
            "platform": "linux/arm64",
            "runnerSha256": "1" * 64,
            "targetUid": 65532,
            "targetGid": 65532,
            "consumerGid": os.getegid(),
            "capabilitySeconds": 300,
            "maxSourceBundleBytes": 64 * 1024 * 1024,
            "profile": copy.deepcopy(self.profile),
        }

    def request_value(self):
        target = {
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
            "changed_files": 1,
            "diff_sha256": "d" * 64,
            "diff_bytes": 128,
            "trusted_context_sha256": "e" * 64,
            "trusted_context_bytes": 64,
            "object_bundle_sha256": hashlib.sha256(self.source_bytes).hexdigest(),
            "object_bundle_bytes": len(self.source_bytes),
        }
        return {
            "version": 1,
            "sessionId": str(uuid.UUID("71f9b79c-9b2a-4b89-a69c-f3ed3a032abc")),
            "challenge": "7" * 64,
            "target": target,
            "action": {"seq": 1, "beat": 1, "attempt": 1},
            "job": copy.deepcopy(self.profile["job"]),
            "sandbox": copy.deepcopy(self.profile["sandbox"]),
            "exitCode": self.profile["exitCode"],
        }

    def write_configuration(self, value=None, *, raw=None, name="adapter.json"):
        path = self.root / name
        path.write_bytes(canonical(value or self.configuration) if raw is None else raw)
        path.chmod(0o600)
        return path

    def stored_execution(self, artifact_names=None):
        values = {
            "sourceBundle": self.source_bytes,
            "outputBundle": b"verified output bundle\n",
            "stdout": b"implementation complete\n",
            "stderr": b"",
            "diagnostic": b"must not be accepted\n",
        }
        names = (
            ("sourceBundle", "outputBundle", "stdout", "stderr")
            if artifact_names is None
            else artifact_names
        )
        artifacts = tuple(
            (name, self.content.put_bytes(values[name])) for name in names
        )
        return StoredExecution(
            request=canonical(self.request),
            capability=b'{"signed":"capability"}',
            receipt=b'{"signed":"receipt"}',
            artifacts=artifacts,
            validation=b'{"status":"accepted"}',
        )

    def assert_no_publish(self, parent=None):
        parent = parent or self.export_root
        self.assertFalse(self.evidence.exists())
        self.assertEqual(list(parent.iterdir()), [])

    def mock_runtime(self, stored=None, *, executor_id=None, key_id=None):
        stored = stored or self.stored_execution()
        signer = SimpleNamespace(
            signer_id=self.profile["signerId"],
            key_id=key_id or self.profile["keyId"],
            algorithm="ecdsa-p256-sha256",
        )
        policy = SimpleNamespace(
            signer_id=self.profile["signerId"],
            executor_id=executor_id or self.profile["executorId"],
            executable=copy.deepcopy(self.profile["job"]["executable"]),
            environment=copy.deepcopy(self.profile["job"]["environment"]),
            sandbox=copy.deepcopy(self.profile["sandbox"]),
            image=self.configuration["image"],
            platform=self.configuration["platform"],
            docker_host=self.configuration["dockerHost"],
            deployment_id=self.configuration["deploymentId"],
            runtime_domain_id=self.configuration["runtimeDomainId"],
        )
        runner = mock.Mock(name="runner")
        broker = mock.Mock(name="broker")
        broker.content = self.content
        broker.execute.return_value = stored
        stack = {
            "signer": mock.patch.object(
                adapter,
                "OpenSSLSigner",
                return_value=signer,
            ),
            "policy": mock.patch.object(adapter, "GatewayPolicy", return_value=policy),
            "runner": mock.patch.object(adapter, "DockerRunner", return_value=runner),
            "broker": mock.patch.object(adapter, "ExecutionBroker", return_value=broker),
        }
        return signer, policy, runner, broker, stack


class ConfigurationTests(GatewayAdapterCase):
    def test_adapter_source_participates_in_the_executor_identity(self):
        names = []
        original = Path.read_bytes

        def tracking(path):
            names.append(path.name)
            return original(path)

        with mock.patch.object(Path, "read_bytes", tracking):
            adapter._policy(self.configuration)

        self.assertIn("adapter.py", names)

    def test_configuration_requires_the_exact_private_version_one_shape(self):
        path = self.write_configuration()
        self.assertIsNotNone(adapter.load_configuration(str(path)))

        invalid = []
        unknown = copy.deepcopy(self.configuration)
        unknown["approval"] = "repository supplied approval"
        invalid.append(("unknown", unknown))
        missing = copy.deepcopy(self.configuration)
        del missing["profile"]
        invalid.append(("missing", missing))
        version = copy.deepcopy(self.configuration)
        version["version"] = 2
        invalid.append(("version", version))
        relative_key = copy.deepcopy(self.configuration)
        relative_key["privateKey"] = "private.pem"
        invalid.append(("relative-key", relative_key))
        relative_store = copy.deepcopy(self.configuration)
        relative_store["storeRoot"] = "gateway-store"
        invalid.append(("relative-store", relative_store))
        ambient_docker = copy.deepcopy(self.configuration)
        ambient_docker["docker"] = "docker"
        invalid.append(("ambient-docker", ambient_docker))

        for index, (label, value) in enumerate(invalid):
            with self.subTest(label=label):
                candidate = self.write_configuration(
                    value, name="invalid-%d.json" % index
                )
                with self.assertRaises(adapter.AdapterError):
                    adapter.load_configuration(str(candidate))

    def test_configuration_rejects_duplicate_fields_and_unsafe_files(self):
        body = canonical(self.configuration)
        duplicate = b'{"version":1,' + body[1:]
        with self.assertRaises(adapter.AdapterError):
            adapter.load_configuration(
                str(self.write_configuration(raw=duplicate, name="duplicate.json"))
            )

        original = self.write_configuration(name="original.json")
        link = self.root / "linked.json"
        link.symlink_to(original)
        with self.assertRaises(adapter.AdapterError):
            adapter.load_configuration(str(link))

        hard_link = self.root / "hard-linked.json"
        os.link(original, hard_link)
        with self.assertRaises(adapter.AdapterError):
            adapter.load_configuration(str(hard_link))

        exposed = self.write_configuration(name="exposed.json")
        exposed.chmod(0o644)
        with self.assertRaises(adapter.AdapterError):
            adapter.load_configuration(str(exposed))

    def test_configuration_requires_protected_directory_ancestry(self):
        exposed = self.root / "exposed-directory"
        exposed.mkdir()
        exposed.chmod(0o777)
        with self.assertRaisesRegex(adapter.AdapterError, "directory ancestry"):
            adapter.load_configuration(
                str(self.write_configuration(name="exposed-directory/adapter.json"))
            )

        sticky = self.root / "sticky-directory"
        sticky.mkdir()
        sticky.chmod(0o1777)
        self.assertIsNotNone(
            adapter.load_configuration(
                str(self.write_configuration(name="sticky-directory/adapter.json"))
            )
        )

    def test_configuration_is_bounded_before_json_decoding(self):
        huge = copy.deepcopy(self.configuration)
        huge["deploymentId"] = "x" * 1_100_000
        with self.assertRaises(adapter.AdapterError):
            adapter.load_configuration(
                str(self.write_configuration(huge, name="huge.json"))
            )

    def test_json_inputs_reject_unsafe_numbers_depth_and_file_aliases(self):
        malformed = (
            b'{"version":' + b"9" * 100 + b"}",
            b'{"value":' + b"[" * 40 + b"0" + b"]" * 40 + b"}",
            b'{"version":1.0}',
        )
        for index, body in enumerate(malformed):
            with self.subTest(index=index):
                path = self.write_configuration(
                    raw=body, name="malformed-%d.json" % index
                )
                with self.assertRaises(adapter.AdapterError):
                    adapter.load_configuration(str(path))

        policy = adapter._policy(self.configuration)
        request_link = self.root / "request-link.json"
        request_link.symlink_to(self.request_path)
        with self.assertRaises(adapter.AdapterError):
            adapter._request(str(request_link), policy, self.profile)

        request_hard_link = self.root / "request-hard-link.json"
        os.link(self.request_path, request_hard_link)
        with self.assertRaises(adapter.AdapterError):
            adapter._request(str(request_hard_link), policy, self.profile)


class EvidencePublicationTests(GatewayAdapterCase):
    def test_publish_exports_exact_private_consumer_evidence(self):
        stored = self.stored_execution()

        adapter.publish_evidence(stored, self.content, str(self.evidence))

        self.assertEqual({path.name for path in self.evidence.iterdir()}, EVIDENCE_FILES)
        expected = {
            "request.json": stored.request,
            "capability.dsse.json": stored.capability,
            "receipt.dsse.json": stored.receipt,
            "output.bundle": self.content.read(stored.artifact("outputBundle")),
            "stdout": self.content.read(stored.artifact("stdout")),
            "stderr": self.content.read(stored.artifact("stderr")),
        }
        self.assertEqual(
            {path.name: path.read_bytes() for path in self.evidence.iterdir()},
            expected,
        )
        self.assertEqual(stat.S_IMODE(self.evidence.stat().st_mode), 0o750)
        for path in self.evidence.iterdir():
            details = path.stat()
            self.assertTrue(stat.S_ISREG(details.st_mode))
            self.assertEqual(details.st_nlink, 1)
            self.assertEqual(details.st_gid, os.getegid())
            self.assertEqual(stat.S_IMODE(details.st_mode), 0o640)
        self.assertNotIn("source.bundle", expected)
        self.assertNotIn("validation.json", expected)

    def test_publish_is_invisible_until_complete_and_cleans_interruptions(self):
        stored = self.stored_execution()
        original_write = os.write
        observed = []

        def tracking_write(descriptor, data):
            observed.append(self.evidence.exists())
            return original_write(descriptor, data)

        with mock.patch.object(adapter.os, "write", side_effect=tracking_write):
            adapter.publish_evidence(stored, self.content, str(self.evidence))
        self.assertTrue(observed)
        self.assertFalse(any(observed))

        for index, failure in enumerate((OSError("disk stopped"), KeyboardInterrupt())):
            with self.subTest(failure=type(failure).__name__):
                parent = self.root / ("failed-export-%d" % index)
                parent.mkdir(mode=0o710)
                destination = parent / "attempt-1"
                calls = []

                def interrupted_write(descriptor, data):
                    calls.append(None)
                    if len(calls) == 2:
                        raise failure
                    return original_write(descriptor, data)

                with mock.patch.object(
                    adapter.os, "write", side_effect=interrupted_write
                ):
                    with self.assertRaises(type(failure)):
                        adapter.publish_evidence(stored, self.content, str(destination))
                self.assertFalse(destination.exists())
                self.assertEqual(list(parent.iterdir()), [])

                adapter.publish_evidence(stored, self.content, str(destination))
                self.assertEqual(
                    {path.name for path in destination.iterdir()}, EVIDENCE_FILES
                )

    def test_publish_recovers_when_parent_fsync_fails_after_rename(self):
        stored = self.stored_execution()
        original_fsync = os.fsync
        calls = []

        def interrupted_fsync(descriptor):
            calls.append(descriptor)
            if len(calls) == 9:
                raise OSError("parent durability failed")
            return original_fsync(descriptor)

        with mock.patch.object(adapter.os, "fsync", side_effect=interrupted_fsync):
            with self.assertRaises(OSError):
                adapter.publish_evidence(stored, self.content, str(self.evidence))
            self.assertTrue(self.evidence.is_dir())
            before = {path.name: path.stat().st_ino for path in self.evidence.iterdir()}
            adapter.publish_evidence(stored, self.content, str(self.evidence))
        self.assertEqual(len(calls), 11)
        after = {path.name: path.stat().st_ino for path in self.evidence.iterdir()}
        self.assertEqual(after, before)

    def test_publish_recovers_when_interrupted_before_consumer_handoff(self):
        stored = self.stored_execution()
        original_fchmod = os.fchmod
        calls = []

        def interrupted_fchmod(descriptor, mode):
            calls.append(mode)
            if len(calls) == 8:
                raise OSError("process stopped before consumer handoff")
            return original_fchmod(descriptor, mode)

        with mock.patch.object(adapter.os, "fchmod", side_effect=interrupted_fchmod):
            with self.assertRaises(OSError):
                adapter.publish_evidence(stored, self.content, str(self.evidence))

        self.assertEqual(stat.S_IMODE(self.evidence.stat().st_mode), 0o700)
        adapter.publish_evidence(stored, self.content, str(self.evidence))
        self.assertEqual(stat.S_IMODE(self.evidence.stat().st_mode), 0o750)
        self.assertEqual({path.name for path in self.evidence.iterdir()}, EVIDENCE_FILES)

    def test_restrictive_umask_cannot_strand_an_unrecoverable_orphan(self):
        stored = self.stored_execution()
        previous = os.umask(0o777)
        try:
            with mock.patch.object(
                adapter.os,
                "fchmod",
                side_effect=KeyboardInterrupt(),
            ), mock.patch.object(
                adapter,
                "_remove_staging_directory",
                return_value=False,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    adapter.publish_evidence(
                        stored,
                        self.content,
                        str(self.evidence),
                    )

            orphan = self.export_root / adapter._staging_name(self.evidence.name)
            self.assertEqual(stat.S_IMODE(orphan.stat().st_mode), 0o700)
            adapter.publish_evidence(stored, self.content, str(self.evidence))
            self.assertFalse(orphan.exists())
            self.assertEqual(stat.S_IMODE(self.evidence.stat().st_mode), 0o750)
        finally:
            active = os.umask(previous)
        self.assertEqual(active, 0o777)

    def test_exact_replay_is_unchanged_and_conflicts_never_overwrite(self):
        stored = self.stored_execution()
        adapter.publish_evidence(stored, self.content, str(self.evidence))
        before = {
            path.name: (
                path.stat().st_ino,
                path.stat().st_mtime_ns,
                path.read_bytes(),
            )
            for path in self.evidence.iterdir()
        }

        adapter.publish_evidence(stored, self.content, str(self.evidence))

        after = {
            path.name: (
                path.stat().st_ino,
                path.stat().st_mtime_ns,
                path.read_bytes(),
            )
            for path in self.evidence.iterdir()
        }
        self.assertEqual(after, before)

        def changed(directory):
            (directory / "stdout").write_bytes(b"changed")

        def missing(directory):
            (directory / "stderr").unlink()

        def extra(directory):
            (directory / "extra").write_bytes(b"extra")

        def linked(directory):
            path = directory / "stdout"
            original = path.read_bytes()
            path.unlink()
            external = directory.parent / (directory.name + "-external")
            external.write_bytes(original)
            path.symlink_to(external)

        def exposed(directory):
            (directory / "stdout").chmod(0o644)

        def hard_linked(directory):
            path = directory / "stdout"
            external = directory.parent / (directory.name + "-hard-link")
            os.link(path, external)

        for index, mutation in enumerate(
            (changed, missing, extra, linked, exposed, hard_linked)
        ):
            with self.subTest(mutation=mutation.__name__):
                parent = self.root / ("conflict-%d" % index)
                parent.mkdir(mode=0o710)
                destination = parent / "attempt-1"
                adapter.publish_evidence(stored, self.content, str(destination))
                mutation(destination)
                names = {path.name for path in destination.iterdir()}
                with self.assertRaises(adapter.AdapterError):
                    adapter.publish_evidence(stored, self.content, str(destination))
                self.assertEqual({path.name for path in destination.iterdir()}, names)

        empty = self.root / "empty-parent"
        empty.mkdir(mode=0o710)
        destination = empty / "attempt-1"
        destination.mkdir(mode=0o700)
        with self.assertRaises(adapter.AdapterError):
            adapter.publish_evidence(stored, self.content, str(destination))
        self.assertEqual(list(destination.iterdir()), [])

        symlink_parent = self.root / "symlink-parent"
        symlink_parent.mkdir(mode=0o710)
        actual = symlink_parent / "actual"
        actual.mkdir(mode=0o700)
        destination = symlink_parent / "attempt-1"
        destination.symlink_to(actual)
        with self.assertRaises(adapter.AdapterError):
            adapter.publish_evidence(stored, self.content, str(destination))
        self.assertTrue(destination.is_symlink())

    def test_publish_requires_exact_stored_artifacts_and_a_private_parent(self):
        cases = (
            ("missing", ("sourceBundle", "stdout", "stderr")),
            (
                "extra",
                ("sourceBundle", "outputBundle", "stdout", "stderr", "diagnostic"),
            ),
            (
                "duplicate",
                ("sourceBundle", "outputBundle", "stdout", "stderr", "stdout"),
            ),
        )
        for index, (label, names) in enumerate(cases):
            with self.subTest(label=label):
                destination = self.export_root / ("bad-%d" % index)
                with self.assertRaises(adapter.AdapterError):
                    adapter.publish_evidence(
                        self.stored_execution(names), self.content, str(destination)
                    )
                self.assertFalse(destination.exists())

        exposed_parent = self.root / "exposed-parent"
        exposed_parent.mkdir(mode=0o700)
        exposed_parent.chmod(0o755)
        destination = exposed_parent / "attempt-1"
        with self.assertRaises(adapter.AdapterError):
            adapter.publish_evidence(
                self.stored_execution(), self.content, str(destination)
            )
        self.assertFalse(destination.exists())

    def test_store_separation_uses_open_directory_identity(self):
        boundary = self.root / "identity-boundary"
        boundary.mkdir(mode=0o710)
        store = boundary / "store"
        store.mkdir(mode=0o700)
        nested = store / "outbox"
        nested.mkdir(mode=0o710)

        with self.assertRaisesRegex(adapter.AdapterError, "must be separate"):
            adapter._preflight_destination(str(store), str(store))
        with self.assertRaisesRegex(adapter.AdapterError, "not traversable"):
            adapter._preflight_destination(str(nested / "attempt-1"), str(store))

        container = boundary / "container"
        container.mkdir(mode=0o750)
        contained_store = container / "store"
        contained_store.mkdir(mode=0o700)
        with self.assertRaisesRegex(adapter.AdapterError, "must be separate"):
            adapter._preflight_destination(str(container), str(contained_store))

        alternate = boundary / "STORE"
        if alternate.exists() and os.path.samefile(alternate, store):
            with self.assertRaises(adapter.AdapterError):
                adapter._preflight_destination(
                    str(alternate / "outbox" / "attempt-1"),
                    str(store),
                )

        sibling = boundary / "evidence"
        sibling.mkdir(mode=0o710)
        self.assertEqual(
            adapter._preflight_destination(str(sibling / "attempt-1"), str(store)),
            str(sibling / "attempt-1"),
        )

    def test_preflight_requires_consumer_traversal_through_every_ancestor(self):
        blocked = self.root / "gateway-only"
        blocked.mkdir(mode=0o700)
        outbox = blocked / "outbox"
        outbox.mkdir(mode=0o710)

        with self.assertRaisesRegex(adapter.AdapterError, "not traversable"):
            adapter._preflight_destination(
                str(outbox / "attempt-1"),
                str(self.store_root),
            )

    def test_publish_reclaims_only_recognizable_crash_staging(self):
        orphan = self.export_root / adapter._staging_name(self.evidence.name)
        orphan.mkdir(mode=0o700)
        partial = orphan / "request.json"
        partial.write_bytes(b"partial")
        partial.chmod(0o600)

        adapter.publish_evidence(
            self.stored_execution(), self.content, str(self.evidence)
        )

        self.assertFalse(orphan.exists())
        self.assertEqual({path.name for path in self.evidence.iterdir()}, EVIDENCE_FILES)

        reserved = self.export_root / (adapter._STAGING_PREFIX + "1" * 32)
        with self.assertRaisesRegex(adapter.AdapterError, "reserved staging"):
            adapter.publish_evidence(
                self.stored_execution(), self.content, str(reserved)
            )
        self.assertFalse(reserved.exists())


class AdapterRunTests(GatewayAdapterCase):
    def test_run_wires_one_dedicated_broker_and_closes_the_runner(self):
        config_path = self.write_configuration()
        stored = self.stored_execution()
        signer, policy, runner, broker, patches = self.mock_runtime(stored)
        with patches["signer"] as signer_class, patches["policy"] as policy_class, patches[
            "runner"
        ] as runner_class, patches["broker"] as broker_class:
            result = adapter.run(
                str(config_path),
                str(self.request_path),
                str(self.source_bundle),
                str(self.evidence),
            )

        self.assertEqual(
            result,
            {
                "version": 1,
                "status": "complete",
                "evidenceDir": str(self.evidence),
            },
        )
        signer_args, signer_kwargs = signer_class.call_args
        self.assertEqual(Path(signer_args[0]), self.private_key)
        self.assertEqual(Path(signer_args[1]), self.public_key)
        self.assertEqual(signer_args[2], self.profile["signerId"])
        self.assertEqual(Path(signer_kwargs["openssl"]), Path(self.configuration["openssl"]))

        policy_kwargs = policy_class.call_args.kwargs
        self.assertEqual(policy_kwargs["signer_id"], self.profile["signerId"])
        self.assertEqual(policy_kwargs["deployment_id"], self.configuration["deploymentId"])
        self.assertEqual(policy_kwargs["runtime_domain_id"], self.configuration["runtimeDomainId"])
        self.assertEqual(policy_kwargs["executable"], self.profile["job"]["executable"])
        self.assertEqual(policy_kwargs["environment"], self.profile["job"]["environment"])
        self.assertEqual(policy_kwargs["sandbox"], self.profile["sandbox"])

        runner_class.assert_called_once_with(
            self.configuration["image"],
            self.configuration["platform"],
            self.configuration["dockerHost"],
            self.configuration["deploymentId"],
            self.configuration["runtimeDomainId"],
            docker=self.configuration["docker"],
        )
        broker_args, broker_kwargs = broker_class.call_args
        self.assertIs(broker_args[0], policy)
        self.assertIs(broker_args[1], signer)
        self.assertEqual(Path(broker_args[2]), self.store_root)
        self.assertIs(broker_args[3], runner)
        self.assertIs(broker_kwargs["dedicated_process"], True)
        self.assertEqual(Path(broker_kwargs["git"]), Path(self.configuration["git"]))
        executed_request, executed_source = broker.execute.call_args.args
        self.assertEqual(executed_request, self.request)
        self.assertEqual(Path(executed_source), self.source_bundle)
        runner.close.assert_called_once_with()
        self.assertEqual({path.name for path in self.evidence.iterdir()}, EVIDENCE_FILES)

    def test_full_profile_and_request_mismatch_fail_before_docker(self):
        cases = []
        wrong_job = copy.deepcopy(self.request)
        wrong_job["job"]["argv"].append("--unexpected")
        cases.append(("job", wrong_job, None, None))
        wrong_sandbox = copy.deepcopy(self.request)
        wrong_sandbox["sandbox"]["limits"]["wallSeconds"] = 30
        cases.append(("sandbox", wrong_sandbox, None, None))
        wrong_exit = copy.deepcopy(self.request)
        wrong_exit["exitCode"] = 7
        cases.append(("exit", wrong_exit, None, None))
        cases.append(
            (
                "key",
                copy.deepcopy(self.request),
                None,
                "sha256:" + "9" * 64,
            )
        )
        cases.append(
            (
                "executor",
                copy.deepcopy(self.request),
                "urn:underwrite:executor:other",
                None,
            )
        )

        for index, (label, request, executor_id, key_id) in enumerate(cases):
            with self.subTest(label=label):
                request_path = self.root / ("request-%d.json" % index)
                request_path.write_bytes(canonical(request))
                request_path.chmod(0o600)
                config_path = self.write_configuration(
                    name="configuration-%d.json" % index
                )
                _signer, _policy, _runner, _broker, patches = self.mock_runtime(
                    executor_id=executor_id,
                    key_id=key_id,
                )
                with patches["signer"], patches["policy"], patches[
                    "runner"
                ] as runner_class, patches["broker"] as broker_class:
                    with self.assertRaises((adapter.AdapterError, adapter.GatewayError)):
                        adapter.run(
                            str(config_path),
                            str(request_path),
                            str(self.source_bundle),
                            str(self.export_root / ("mismatch-%d" % index)),
                        )
                runner_class.assert_not_called()
                broker_class.assert_not_called()

    def test_broker_or_publish_failure_closes_runner_without_partial_evidence(self):
        config_path = self.write_configuration()
        stored = self.stored_execution()
        _signer, _policy, runner, broker, patches = self.mock_runtime(stored)
        broker.execute.side_effect = adapter.AdapterError("gateway failed")
        with patches["signer"], patches["policy"], patches["runner"], patches[
            "broker"
        ]:
            with self.assertRaises(adapter.AdapterError):
                adapter.run(
                    str(config_path),
                    str(self.request_path),
                    str(self.source_bundle),
                    str(self.evidence),
                )
        runner.close.assert_called_once_with()
        self.assert_no_publish()

        _signer, _policy, runner, _broker, patches = self.mock_runtime(stored)
        with patches["signer"], patches["policy"], patches["runner"], patches[
            "broker"
        ], mock.patch.object(
            adapter,
            "publish_evidence",
            side_effect=KeyboardInterrupt(),
        ):
            with self.assertRaises(KeyboardInterrupt):
                adapter.run(
                    str(config_path),
                    str(self.request_path),
                    str(self.source_bundle),
                    str(self.evidence),
                )
        runner.close.assert_called_once_with()
        self.assert_no_publish()


class CliOutcomeTests(unittest.TestCase):
    arguments = [
        "--config",
        "/private/config.json",
        "--request",
        "/private/request.json",
        "--source-bundle",
        "/private/source.bundle",
        "--evidence-dir",
        "/private/outbox/attempt-1",
    ]

    def invoke(self, *, result=None, error=None):
        stdout = SimpleNamespace(buffer=io.BytesIO())
        stderr = io.StringIO()
        call = mock.patch.object(
            adapter,
            "run",
            return_value=result,
            side_effect=error,
        )
        with call, mock.patch.object(
            adapter.sys,
            "stdout",
            stdout,
        ), mock.patch.object(
            adapter.sys,
            "stderr",
            stderr,
        ):
            code = adapter.main(self.arguments)
        return code, json.loads(stdout.buffer.getvalue()), stderr.getvalue()

    def test_cli_reports_complete_retry_and_definite_failure_as_json(self):
        complete = {
            "version": 1,
            "status": "complete",
            "evidenceDir": "/private/outbox/attempt-1",
        }
        code, result, detail = self.invoke(result=complete)
        self.assertEqual((code, result, detail), (0, complete, ""))

        code, result, detail = self.invoke(
            error=adapter.GatewayError("Docker is unavailable")
        )
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "retry")
        self.assertIn("Docker is unavailable", detail)

        for error in (
            adapter.AttemptFailed("sandbox failed"),
            adapter.ReplayConflict("attempt is executing", state="executing"),
        ):
            with self.subTest(error=type(error).__name__):
                code, result, detail = self.invoke(error=error)
                self.assertEqual(code, 2)
                self.assertEqual(result["status"], "failed")
                self.assertIn(str(error), detail)

        code, result, _detail = self.invoke(
            error=adapter.ReplayConflict("identity was reused")
        )
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "retry")


if __name__ == "__main__":
    unittest.main()
