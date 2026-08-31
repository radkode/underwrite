#!/usr/bin/env python3
"""Contracts for linked PR implementation through signed gateway evidence."""

import copy
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "underwrite" / "scripts"
for path in (ROOT, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import attested_implementation as bridge  # noqa: E402
import execution_receipt  # noqa: E402
from gateway import artifacts  # noqa: E402
from gateway.signing import OpenSSLSigner  # noqa: E402
from pr_snapshot import SnapshotError, TargetMoved, capture  # noqa: E402
from session_store import Conflict, SessionStore  # noqa: E402


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class AttestedImplementationCase(unittest.TestCase):
    def setUp(self):
        if shutil.which("openssl") is None:
            self.skipTest("OpenSSL is required")
        self.temporary = tempfile.TemporaryDirectory(prefix="underwrite-attested-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.remote = self.root / "upstream.git"
        self.controller = self.root / "controller"
        self.repo = self.root / "implementation-repo"
        self.source_root = self.root / "source-session"
        self.git("init", "--bare", "--template=", self.remote)
        self.git("init", "--template=", self.controller)
        self.configure_repo(self.controller)
        (self.controller / "app.py").write_text("VALUE = 0\n", encoding="utf-8")
        self.git("add", "app.py", cwd=self.controller)
        self.git("commit", "-m", "base", cwd=self.controller)
        self.git("branch", "-M", "main", cwd=self.controller)
        self.git("remote", "add", "origin", self.remote, cwd=self.controller)
        self.git("push", "origin", "main", cwd=self.controller)
        self.base = self.git("rev-parse", "HEAD", cwd=self.controller)

        self.git("checkout", "-b", "feature", cwd=self.controller)
        (self.controller / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.git("add", "app.py", cwd=self.controller)
        self.git("commit", "-m", "feature", cwd=self.controller)
        self.head = self.git("rev-parse", "HEAD", cwd=self.controller)
        self.git("push", "origin", "HEAD:refs/pull/7/head", cwd=self.controller)
        self.git("checkout", "main", cwd=self.controller)

        self.metadata = {
            "number": 7,
            "state": "open",
            "merged_at": None,
            "changed_files": 1,
            "base": {
                "ref": "main",
                "sha": self.base,
                "repo": {
                    "full_name": "acme/widget",
                    "clone_url": str(self.remote),
                },
            },
            "head": {
                "ref": "feature",
                "sha": self.head,
                "repo": {"id": 123, "full_name": "acme/widget"},
            },
        }
        self.source = SessionStore(self.source_root)
        capture(
            self.source,
            "acme/widget",
            7,
            self.controller,
            api=self.api,
        )
        self.source.patch_session({"repo": "acme/widget", "cursor": 1})
        self.source.put_beat({
            "n": 1,
            "tier": "core",
            "state": "flag",
            "claim": "the value is not pinned",
            "where": "app.py:1",
            "slots": {
                "what": "the value is not pinned",
                "proof": "app.py:1",
                "risk": "the value can change unexpectedly",
                "fix": "pin the value",
            },
        })
        action = self.source.produce(
            "source-review-accept",
            1,
            "accept",
            "include the finding",
        )
        self.action_seq = action["seq"]
        self.source.ack(self.action_seq)

        self.git("init", "--template=", self.repo)
        self.configure_repo(self.repo)
        self.private_key = self.root / "private.pem"
        self.public_key = self.root / "public.pem"
        self.command(
            "openssl",
            "genpkey",
            "-algorithm",
            "EC",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-pkeyopt",
            "ec_param_enc:named_curve",
            "-out",
            self.private_key,
        )
        self.private_key.chmod(0o600)
        self.public_key.write_bytes(
            self.command(
                "openssl",
                "pkey",
                "-in",
                self.private_key,
                "-pubout",
                stdout=True,
            )
        )
        self.signer_id = "urn:underwrite:signer:test"
        self.signer = OpenSSLSigner(
            self.private_key,
            self.public_key,
            self.signer_id,
        )
        self.workspace_limit = 4 * 1024 * 1024
        self.profile = {
            "version": 1,
            "keyId": self.signer.key_id,
            "signerId": self.signer_id,
            "executorId": "urn:underwrite:executor:test",
            "job": {
                "argv": ["/usr/bin/python3", "-I", "implement.py"],
                "cwd": ".",
                "environment": {"LANG": "C"},
                "executable": {
                    "path": "/usr/bin/python3",
                    "sha256": "4" * 64,
                    "bytes": 1,
                },
                "stdin": "closed",
            },
            "sandbox": {
                "policy": execution_receipt.SANDBOX_POLICY_TYPE,
                "credentials": "absent",
                "network": "denied",
                "hostWrites": "denied",
                "gitHooks": "disabled",
                "gitFilters": "disabled",
                "timeout": "enforced",
                "limits": {
                    "wallSeconds": 60,
                    "cpuSeconds": 60,
                    "memoryBytes": 64 * 1024 * 1024,
                    "processes": 1,
                    "workspaceBytes": self.workspace_limit,
                    "outputBytes": 1024 * 1024,
                },
            },
            "exitCode": 0,
        }
        self.now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)

    def command(self, *command, cwd=None, stdout=False):
        environment = {
            name: value for name, value in os.environ.items() if not name.startswith("GIT_")
        }
        environment.update({
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        })
        completed = subprocess.run(
            [str(part) for part in command],
            cwd=None if cwd is None else str(cwd),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", "replace"),
        )
        return completed.stdout if stdout else completed.stdout.decode().strip()

    def git(self, *arguments, cwd=None):
        return self.command("git", *arguments, cwd=cwd)

    def configure_repo(self, repo):
        self.git("config", "user.name", "Underwrite Test", cwd=repo)
        self.git("config", "user.email", "underwrite@example.test", cwd=repo)
        self.git("config", "commit.gpgsign", "false", cwd=repo)

    def api(self, _repo, _number):
        return copy.deepcopy(self.metadata)

    def link(self):
        return bridge.link(
            self.source_root,
            self.repo,
            seq=self.action_seq,
            beat=1,
            actor="reviewer",
            approval="implement the accepted fix",
            api=self.api,
        )

    def linked(self):
        result = self.link()
        return result, Path(result["child_root"]), SessionStore(result["child_root"])

    def output_bundle(self, value):
        source_bytes = self.source.read_object_bundle()
        target = self.source.frozen_target()
        temporary = tempfile.TemporaryDirectory(prefix="underwrite-test-output-")
        self.addCleanup(temporary.cleanup)
        workspace = Path(temporary.name) / "workspace"
        source = artifacts.verify_source_bundle(
            source_bytes,
            target,
            workspace,
            maximum_workspace_bytes=self.workspace_limit,
        )
        (workspace / "app.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
        built = artifacts.build_output_bundle(
            workspace,
            maximum_workspace_bytes=self.workspace_limit,
            maximum_bundle_bytes=self.workspace_limit * 2,
        )
        return source.git_tree, built

    def evidence(self, child_root, request_value, value=2, signed_value=None):
        input_tree, built = self.output_bundle(value)
        signed = built
        if signed_value is not None:
            signed_input, signed = self.output_bundle(signed_value)
            self.assertEqual(signed_input, input_tree)
        stdout = b"implementation complete\n"
        stderr = b""
        expected = {
            "signerId": self.profile["signerId"],
            "executorId": self.profile["executorId"],
            "sessionId": request_value["sessionId"],
            "challenge": request_value["challenge"],
            "target": copy.deepcopy(request_value["target"]),
            "action": copy.deepcopy(request_value["action"]),
            "job": copy.deepcopy(self.profile["job"]),
            "inputTree": input_tree,
            "sandbox": copy.deepcopy(self.profile["sandbox"]),
            "outputTree": signed.descriptor.git_tree,
            "outputBundle": {
                "sha256": signed.descriptor.sha256,
                "bytes": signed.descriptor.bytes,
            },
            "stdout": {
                "sha256": hashlib.sha256(stdout).hexdigest(),
                "bytes": len(stdout),
                "truncated": False,
            },
            "stderr": {
                "sha256": hashlib.sha256(stderr).hexdigest(),
                "bytes": len(stderr),
                "truncated": False,
            },
            "exitCode": 0,
        }
        capability_expected = {
            name: expected[name]
            for name in (
                "signerId",
                "executorId",
                "sessionId",
                "challenge",
                "target",
                "action",
                "job",
                "inputTree",
                "sandbox",
            )
        }
        capability_payload = execution_receipt.build_host_capability_payload(
            capability_expected,
            self.now - timedelta(seconds=5),
            self.now + timedelta(minutes=4),
        )
        capability = execution_receipt.build_dsse_envelope(
            capability_payload,
            self.signer.key_id,
            self.signer.sign,
        )
        receipt_payload = execution_receipt.build_execution_receipt_payload(
            capability_payload,
            expected,
            self.now - timedelta(seconds=4),
            self.now - timedelta(seconds=3),
        )
        receipt = execution_receipt.build_dsse_envelope(
            receipt_payload,
            self.signer.key_id,
            self.signer.sign,
        )
        directory = self.root / f"evidence-{request_value['action']['attempt']}"
        directory.mkdir()
        values = {
            "request.json": canonical(request_value),
            "capability.dsse.json": capability,
            "receipt.dsse.json": receipt,
            "output.bundle": built.bundle,
            "stdout": stdout,
            "stderr": stderr,
        }
        for name, data in values.items():
            (directory / name).write_bytes(data)
        return directory, built.descriptor

    def verified(self):
        result, child_root, child = self.linked()
        request_value = bridge.request(
            child_root,
            self.source_root,
            self.profile,
            api=self.api,
        )
        directory, descriptor = self.evidence(child_root, request_value)
        attempt = bridge.consume(
            child_root,
            self.source_root,
            self.profile,
            self.public_key,
            directory,
            now=self.now,
        )
        return result, child_root, child, request_value, attempt, descriptor

    def branch_head(self, branch):
        return self.git("rev-parse", f"refs/heads/{branch}", cwd=self.repo)

    def another_public_key(self):
        private_key = self.root / "other-private.pem"
        public_key = self.root / "other-public.pem"
        self.command(
            "openssl",
            "genpkey",
            "-algorithm",
            "EC",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-pkeyopt",
            "ec_param_enc:named_curve",
            "-out",
            private_key,
        )
        private_key.chmod(0o600)
        public_key.write_bytes(
            self.command(
                "openssl",
                "pkey",
                "-in",
                private_key,
                "-pubout",
                stdout=True,
            )
        )
        return public_key

    def test_link_is_idempotent_preserves_source_and_seeds_exact_head(self):
        before = self.source.snapshot()

        first, child_root, child = self.linked()
        second = self.link()

        self.assertEqual(first, second)
        self.assertEqual(self.source.snapshot(), before)
        self.assertEqual(child.frozen_target(), self.source.frozen_target())
        self.assertEqual(self.branch_head(first["branch"]), self.head)
        linked = child.snapshot()[0]["linked_implementation"]
        self.assertEqual(linked["source_session_id"], self.source.delivery_state()["session_id"])
        self.assertEqual(linked["source_beat"], 1)
        self.assertEqual(
            child_root.parent.resolve(),
            (self.source_root / "implementations").resolve(),
        )
        self.git("checkout", first["branch"], cwd=self.repo)
        ref = "refs/heads/" + first["branch"]
        self.assertTrue(bridge._checked_out(self.repo, ref))
        grouped = b"worktree /tmp/example\nHEAD " + self.head.encode() + b"\nbranch " + ref.encode() + b"\n\n"
        with mock.patch.object(bridge, "_git", return_value=grouped):
            self.assertTrue(bridge._checked_out(self.repo, ref))

    def test_git_commands_pin_durable_object_and_reference_writes(self):
        result = bridge._git_result(self.repo, ["version"])

        self.assertIn("core.fsync=objects,pack-metadata,reference", result.args)
        self.assertIn("core.fsyncMethod=fsync", result.args)

    def test_implementation_repository_requires_git_with_fsync_components(self):
        with mock.patch.object(
            bridge,
            "_git",
            return_value=b"git version 2.35.9\n",
        ):
            with self.assertRaisesRegex(bridge.ImplementationError, "Git 2.36"):
                bridge._repository(self.repo)

    def test_git_timeout_kills_the_complete_process_group(self):
        process = mock.Mock(pid=1234)
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(["git"], bridge._GIT_SECONDS),
            None,
        ]
        with (
            mock.patch.object(subprocess, "Popen", return_value=process) as popen,
            mock.patch.object(os, "killpg") as killpg,
        ):
            with self.assertRaisesRegex(bridge.ImplementationError, "timed out"):
                bridge._git_result(self.repo, ["version"])

        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        killpg.assert_called_once_with(1234, bridge.signal.SIGKILL)

    def test_many_output_entries_use_one_batched_git_tree_pipeline(self):
        self.linked()
        output = self.root / "many-output-files"
        output.mkdir()
        for number in range(300):
            (output / f"file-{number}.txt").write_text(
                f"value {number}\n", encoding="utf-8"
            )
        executable = output / "run.sh"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        (output / "current").symlink_to("file-0.txt")
        expected = artifacts.synthetic_git_tree(
            output,
            maximum_bytes=self.workspace_limit,
        )
        deadline = bridge.time.monotonic() + bridge._LAND_SECONDS

        with mock.patch.object(bridge, "_git", wraps=bridge._git) as git:
            tree = bridge._write_tree(
                self.repo,
                output,
                self.workspace_limit,
                deadline,
            )

        self.assertEqual(git.call_count, 4)
        commit = bridge._commit(
            self.repo,
            tree,
            self.head,
            "test: batch output tree",
            deadline,
        )
        self.assertEqual(
            artifacts.measure_commit_tree(
                self.repo,
                commit,
                maximum_workspace_bytes=self.workspace_limit,
                deadline=deadline,
            ),
            expected,
        )

    def test_unborn_checked_out_implementation_branch_is_not_moved(self):
        authorization = self.source.authorize_implementation(
            self.action_seq,
            1,
            "reviewer",
            "implement the accepted fix",
        )
        branch = authorization["branch"]
        self.git("switch", "--orphan", branch, cwd=self.repo)

        with self.assertRaisesRegex(Conflict, "checked out"):
            self.link()

        self.assertEqual(
            self.git("symbolic-ref", "--short", "HEAD", cwd=self.repo),
            branch,
        )
        self.assertEqual(
            self.git(
                "for-each-ref",
                "--format=%(objectname)",
                f"refs/heads/{branch}",
                cwd=self.repo,
            ),
            "",
        )
        self.assertEqual(self.git("status", "--porcelain", cwd=self.repo), "")

    def test_linked_child_path_cannot_be_replaced_by_a_symlink(self):
        _linked, child_root, _child = self.linked()
        relocated = self.root / "relocated-child"
        child_root.rename(relocated)
        child_root.symlink_to(relocated, target_is_directory=True)

        with self.assertRaisesRegex(bridge.ImplementationError, "real directory"):
            bridge.request(
                child_root,
                self.source_root,
                self.profile,
                api=self.api,
            )

    def test_linked_child_database_symlink_is_rejected_before_external_mutation(self):
        _linked, child_root, _child = self.linked()
        database = child_root / "session.sqlite3"
        external = self.root / "external-session.sqlite3"
        database.rename(external)
        database.symlink_to(external)
        with sqlite3.connect(str(external)) as db:
            before = db.execute("SELECT COUNT(*) FROM implementation_attempts").fetchone()[0]

        with self.assertRaisesRegex(
            bridge.ImplementationError, "database must be one private regular file"
        ):
            bridge.request(
                child_root,
                self.source_root,
                self.profile,
                api=self.api,
            )

        with sqlite3.connect(str(external)) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM implementation_attempts").fetchone()[0],
                before,
            )

    def test_wrong_child_directory_is_rejected_without_migration(self):
        linked, _child_root, _child = self.linked()
        outside = self.root / "outside" / linked["link"]["link_id"]
        outside.mkdir(parents=True)
        (outside / "session.json").write_bytes(b'{"title":"foreign"}\n')
        (outside / "notes.txt").write_bytes(b"keep me\n")
        before = {path.name: path.read_bytes() for path in outside.iterdir()}

        with self.assertRaisesRegex(Conflict, "source-bound path"):
            bridge.request(
                outside,
                self.source_root,
                self.profile,
                api=self.api,
            )

        self.assertEqual(
            {path.name: path.read_bytes() for path in outside.iterdir()},
            before,
        )

    def test_profile_resource_limits_fail_before_attempt_reservation(self):
        _linked, child_root, child = self.linked()
        oversized = copy.deepcopy(self.profile)
        oversized["sandbox"]["limits"]["workspaceBytes"] = 64 * 1024 * 1024 + 1

        with self.assertRaisesRegex(bridge.ImplementationError, "limit is too large"):
            bridge.request(
                child_root,
                self.source_root,
                oversized,
                api=self.api,
            )
        with self.assertRaises(Exception):
            child.implementation_attempt(1)

        unsupported = copy.deepcopy(self.profile)
        unsupported["sandbox"]["limits"]["processes"] = 2
        with self.assertRaisesRegex(bridge.ImplementationError, "process limit"):
            bridge.request(
                child_root,
                self.source_root,
                unsupported,
                api=self.api,
            )

    def test_profile_protocol_limits_fail_before_attempt_reservation(self):
        _linked, child_root, child = self.linked()
        invalid = []
        oversized = copy.deepcopy(self.profile)
        oversized["signerId"] = "x" * bridge.MAX_IMPLEMENTATION_PROFILE_BYTES
        invalid.append(("byte limit", oversized))
        exit_code = copy.deepcopy(self.profile)
        exit_code["exitCode"] = 256
        invalid.append(("exitCode", exit_code))
        cwd = copy.deepcopy(self.profile)
        cwd["job"]["cwd"] = "x" * 256
        invalid.append(("cwd component", cwd))
        cwd = copy.deepcopy(self.profile)
        cwd["job"]["cwd"] = "x" * 4097
        invalid.append(("cwd total", cwd))
        executable_component = copy.deepcopy(self.profile)
        executable_component["job"]["executable"]["path"] = "/" + "x" * 256
        executable_component["job"]["argv"][0] = executable_component["job"][
            "executable"
        ]["path"]
        invalid.append(("executable component", executable_component))
        executable_path = copy.deepcopy(self.profile)
        executable_path["job"]["executable"]["path"] = "/" + "/".join(
            "x" * 255 for _ in range(16)
        )
        executable_path["job"]["argv"][0] = executable_path["job"]["executable"][
            "path"
        ]
        invalid.append(("executable path", executable_path))
        argument = copy.deepcopy(self.profile)
        argument["job"]["argv"].append("x" * (128 * 1024))
        invalid.append(("exec string", argument))
        pointers = copy.deepcopy(self.profile)
        pointers["job"]["argv"].extend([""] * 15_000)
        invalid.append(("exec pointers", pointers))

        for name, profile in invalid:
            with self.subTest(name=name):
                with self.assertRaises((bridge.ImplementationError, execution_receipt.ReceiptError)):
                    bridge.request(
                        child_root,
                        self.source_root,
                        profile,
                        api=self.api,
                    )
                with self.assertRaises(Exception):
                    child.implementation_attempt(1)

    def test_profile_json_rejects_huge_integers_and_deep_nesting(self):
        malformed = {
            "oversized integer": b'{"version":' + b"9" * 5000 + b"}",
            "deep nesting": (
                b'{"version":1,"job":'
                + b"[" * 1100
                + b"0"
                + b"]" * 1100
                + b"}"
            ),
        }
        for name, body in malformed.items():
            with self.subTest(name=name):
                profile = self.root / f"malformed-profile-{name}.json"
                profile.write_bytes(body)
                with self.assertRaises(bridge.ImplementationError):
                    bridge._profile(profile)

    def test_request_json_rejects_huge_integers_and_deep_nesting(self):
        _linked, child_root, child = self.linked()
        request_value = bridge.request(
            child_root,
            self.source_root,
            self.profile,
            api=self.api,
        )
        directory, _descriptor = self.evidence(child_root, request_value)
        malformed = {
            "oversized integer": b'{"action":{"attempt":'
            + b"9" * 5000
            + b"}}",
            "deep nesting": b'{"action":' + b"[" * 1100 + b"0" + b"]" * 1100 + b"}",
        }
        for name, body in malformed.items():
            with self.subTest(name=name):
                (directory / "request.json").write_bytes(body)
                with self.assertRaises(bridge.ImplementationError):
                    bridge.consume(
                        child_root,
                        self.source_root,
                        self.profile,
                        self.public_key,
                        directory,
                        now=self.now,
                    )
                self.assertEqual(
                    child.implementation_attempt(1, 1)["state"],
                    "reserved",
                )

    def test_oversized_source_bundle_is_rejected_before_link_side_effects(self):
        target = copy.deepcopy(self.source.frozen_target())
        target["object_bundle_bytes"] = 64 * 1024 * 1024 + 1
        with (
            mock.patch.object(SessionStore, "frozen_target", return_value=target),
            mock.patch.object(SessionStore, "verify_target_files") as verify,
            mock.patch.object(bridge, "_verify_link_bundle") as quarantine,
        ):
            with self.assertRaisesRegex(bridge.ImplementationError, "gateway byte limit"):
                self.link()
        verify.assert_not_called()
        quarantine.assert_not_called()

        self.assertFalse((self.source_root / "implementations").exists())
        with sqlite3.connect(str(self.source_root / "session.sqlite3")) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM implementation_links").fetchone()[0],
                0,
            )
        self.assertEqual(
            self.git(
                "for-each-ref",
                "--format=%(refname)",
                "refs/heads/underwrite/",
                cwd=self.repo,
            ),
            "",
        )

    def test_oversized_source_diff_is_rejected_before_link_side_effects(self):
        target = copy.deepcopy(self.source.frozen_target())
        target["diff_bytes"] = bridge._MAX_CONSUMER_DIFF_BYTES + 1
        with (
            mock.patch.object(SessionStore, "frozen_target", return_value=target),
            mock.patch.object(SessionStore, "verify_target_files") as verify,
            mock.patch.object(bridge, "_verify_link_bundle") as quarantine,
        ):
            with self.assertRaisesRegex(bridge.ImplementationError, "diff exceeds"):
                self.link()
        verify.assert_not_called()
        quarantine.assert_not_called()

        self.assertFalse((self.source_root / "implementations").exists())
        with sqlite3.connect(str(self.source_root / "session.sqlite3")) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM implementation_links").fetchone()[0],
                0,
            )

    def test_source_without_a_frozen_bundle_is_rejected_without_a_traceback(self):
        with sqlite3.connect(str(self.source_root / "session.sqlite3")) as db:
            body = json.loads(
                db.execute(
                    "SELECT body_json FROM session WHERE singleton = 1"
                ).fetchone()[0]
            )
            body["target"].pop("object_bundle_sha256")
            body["target"].pop("object_bundle_bytes")
            body["execution_policy"]["target"].pop("object_bundle_sha256")
            db.execute(
                "UPDATE session SET body_json = ? WHERE singleton = 1",
                (canonical(body).decode("utf-8"),),
            )
        (self.source_root / "pr.bundle").unlink()

        with self.assertRaisesRegex(Conflict, "requires a frozen Git object bundle"):
            self.link()

        self.assertFalse((self.source_root / "implementations").exists())

    def test_source_bundle_quarantine_failure_precedes_link_side_effects(self):
        with mock.patch.object(
            artifacts,
            "verify_source_bundle",
            side_effect=artifacts.ArtifactError("bad source bundle"),
        ):
            with self.assertRaisesRegex(artifacts.ArtifactError, "bad source bundle"):
                self.link()

        self.assertFalse((self.source_root / "implementations").exists())
        with sqlite3.connect(str(self.source_root / "session.sqlite3")) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM implementation_links").fetchone()[0],
                0,
            )

    def test_pr_move_during_bundle_quarantine_precedes_link_side_effects(self):
        def move_during_quarantine(*_args, **_kwargs):
            self.metadata["head"]["sha"] = "e" * 40

        with mock.patch.object(
            artifacts,
            "verify_source_bundle",
            side_effect=move_during_quarantine,
        ):
            with self.assertRaises(TargetMoved):
                self.link()

        self.assertFalse((self.source_root / "implementations").exists())
        with sqlite3.connect(str(self.source_root / "session.sqlite3")) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM implementation_links").fetchone()[0],
                0,
            )

    def test_checkout_during_live_pr_check_blocks_the_ref_update(self):
        linked, child_root, _child, _request, _attempt, _descriptor = self.verified()

        def checkout(_repo, _number):
            self.git("checkout", linked["branch"], cwd=self.repo)
            return copy.deepcopy(self.metadata)

        with self.assertRaisesRegex(Conflict, "checked out"):
            bridge.land(
                child_root,
                self.source_root,
                self.repo,
                attempt=1,
                message="fix: pin the value",
                api=checkout,
            )
        self.assertEqual(self.branch_head(linked["branch"]), self.head)

    def test_output_tree_mismatch_fails_without_moving_the_branch(self):
        linked, child_root, child = self.linked()
        request_value = bridge.request(
            child_root,
            self.source_root,
            self.profile,
            api=self.api,
        )
        directory, _descriptor = self.evidence(
            child_root,
            request_value,
            value=2,
            signed_value=3,
        )

        with self.assertRaisesRegex(Exception, "tree"):
            bridge.consume(
                child_root,
                self.source_root,
                self.profile,
                self.public_key,
                directory,
                now=self.now,
            )

        attempt = child.implementation_attempt(1, 1)
        self.assertEqual(attempt["state"], "reserved")
        self.assertIsNone(attempt["commit_plan"])
        self.assertEqual(self.branch_head(linked["branch"]), self.head)

    def test_wrong_key_leaves_the_same_evidence_attempt_retryable(self):
        linked, child_root, child = self.linked()
        request_value = bridge.request(
            child_root,
            self.source_root,
            self.profile,
            api=self.api,
        )
        directory, _descriptor = self.evidence(child_root, request_value)

        with self.assertRaisesRegex(Conflict, "key ID"):
            bridge.consume(
                child_root,
                self.source_root,
                self.profile,
                self.another_public_key(),
                directory,
                now=self.now,
            )

        self.assertEqual(child.implementation_attempt(1, 1)["state"], "reserved")
        verified = bridge.consume(
            child_root,
            self.source_root,
            self.profile,
            self.public_key,
            directory,
            now=self.now,
        )
        self.assertEqual(verified["attempt"], 1)
        self.assertEqual(verified["state"], "verified")
        self.assertEqual(self.branch_head(linked["branch"]), self.head)

    def test_untrusted_evidence_fifo_is_rejected_without_blocking(self):
        _linked, child_root, _child = self.linked()
        request_value = bridge.request(
            child_root,
            self.source_root,
            self.profile,
            api=self.api,
        )
        directory, _descriptor = self.evidence(child_root, request_value)
        (directory / "stdout").unlink()
        os.mkfifo(directory / "stdout")
        profile = self.root / "fifo-profile.json"
        profile.write_bytes(canonical(self.profile))
        environment = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith("GIT_")
        }
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "implementationctl.py"),
                "consume",
                str(child_root),
                str(self.source_root),
                str(profile),
                str(self.public_key),
                str(directory),
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn(b"private regular file", completed.stderr)

    def test_untrusted_evidence_directory_rejects_after_one_extra_entry(self):
        directory = self.root / "crowded-evidence"
        directory.mkdir()
        for name in bridge._EVIDENCE_FILES:
            (directory / name).write_bytes(b"")
        for index in range(100):
            (directory / f"extra-{index}").write_bytes(b"")

        with self.assertRaisesRegex(bridge.ImplementationError, "exact file set"):
            bridge._evidence_directory(directory)

    def test_consume_cli_returns_json_after_persisting_verified_evidence(self):
        _linked, child_root, _child = self.linked()
        self.now = datetime.now(timezone.utc)
        request_value = bridge.request(
            child_root,
            self.source_root,
            self.profile,
            api=self.api,
        )
        directory, _descriptor = self.evidence(child_root, request_value)
        profile = self.root / "trusted-profile.json"
        profile.write_bytes(canonical(self.profile))

        output = self.command(
            sys.executable,
            SCRIPTS / "implementationctl.py",
            "consume",
            child_root,
            self.source_root,
            profile,
            self.public_key,
            directory,
        )
        result = json.loads(output)

        self.assertEqual(result["attempt"], 1)
        self.assertEqual(result["state"], "verified")
        self.assertNotIn("capability", result)
        self.assertNotIn("receipt", result)

    def test_reported_gateway_failure_issues_one_new_challenge(self):
        _linked, child_root, _child = self.linked()
        first = bridge.request(
            child_root,
            self.source_root,
            self.profile,
            api=self.api,
        )

        failed = json.loads(
            self.command(
                sys.executable,
                SCRIPTS / "implementationctl.py",
                "fail",
                child_root,
                self.source_root,
                "--attempt",
                "1",
                "--reason",
                "gateway execution failed",
            )
        )
        self.assertEqual(
            bridge.fail(
                child_root,
                self.source_root,
                attempt=1,
                reason="gateway execution failed",
            ),
            failed,
        )
        self.assertEqual(failed["state"], "failed")
        self.assertNotIn("capability", failed)
        self.assertNotIn("receipt", failed)
        json.dumps(failed)

        second = bridge.request(
            child_root,
            self.source_root,
            self.profile,
            api=self.api,
        )
        self.assertEqual(second["action"]["attempt"], 2)
        self.assertNotEqual(second["challenge"], first["challenge"])

    def test_verified_replay_requires_its_persisted_evidence_directory(self):
        _linked, child_root, _child, request_value, _attempt, _descriptor = self.verified()
        persisted = child_root / "implementation-evidence" / "1"
        shutil.rmtree(persisted)

        with self.assertRaisesRegex(Conflict, "persisted implementation evidence is missing"):
            bridge.consume(
                child_root,
                self.source_root,
                self.profile,
                self.public_key,
                self.root / f"evidence-{request_value['action']['attempt']}",
                now=self.now + timedelta(minutes=10),
            )

        self.assertFalse(persisted.exists())

    def test_verified_evidence_lands_one_exact_commit_and_replays(self):
        before = self.source.snapshot()
        linked, child_root, child, request_value, attempt, descriptor = self.verified()
        self.assertEqual(
            bridge.request(child_root, self.source_root, self.profile, api=self.api),
            request_value,
        )
        evidence_dir = self.root / "evidence-1"
        replayed = bridge.consume(
            child_root,
            self.source_root,
            self.profile,
            self.public_key,
            evidence_dir,
            now=self.now + timedelta(minutes=10),
        )
        self.assertEqual(replayed, attempt)
        self.assertNotIn("capability", attempt)
        self.assertNotIn("receipt", attempt)
        json.dumps(attempt)

        first = bridge.land(
            child_root,
            self.source_root,
            self.repo,
            attempt=1,
            message="fix: pin the value",
            api=self.api,
        )
        second = bridge.land(
            child_root,
            self.source_root,
            self.repo,
            attempt=1,
            message="fix: pin the value",
            api=self.api,
        )

        self.assertEqual(first, second)
        commit = first["artifact"]
        self.assertEqual(self.branch_head(linked["branch"]), commit)
        self.assertEqual(
            self.git("rev-parse", f"{commit}^", cwd=self.repo),
            self.head,
        )
        self.assertEqual(
            artifacts.measure_commit_tree(
                self.repo,
                commit,
                maximum_workspace_bytes=self.workspace_limit,
            ),
            descriptor.git_tree,
        )
        self.assertEqual(child.implementation_attempt(1, 1)["state"], "landed")
        self.assertFalse(child.delivery_state()["recovery"])
        self.assertEqual(self.source.snapshot(), before)

    def test_restart_after_ref_update_finishes_the_same_planned_commit(self):
        linked, child_root, child, _request, _attempt, _descriptor = self.verified()
        original = SessionStore.finish_implementation_land
        with mock.patch.object(
            SessionStore,
            "finish_implementation_land",
            side_effect=RuntimeError("process stopped after ref update"),
        ):
            with self.assertRaisesRegex(RuntimeError, "process stopped"):
                bridge.land(
                    child_root,
                    self.source_root,
                    self.repo,
                    attempt=1,
                    message="fix: pin the value",
                    api=self.api,
                )
        prepared = child.implementation_attempt(1, 1)
        self.assertEqual(prepared["state"], "prepared")
        planned = prepared["commit_plan"]["commit"]
        self.assertEqual(self.branch_head(linked["branch"]), planned)

        with mock.patch.object(SessionStore, "finish_implementation_land", original):
            landed = bridge.land(
                child_root,
                self.source_root,
                self.repo,
                attempt=1,
                message="fix: pin the value",
                api=self.api,
            )

        self.assertEqual(landed["artifact"], planned)
        self.assertEqual(child.implementation_attempt(1, 1)["state"], "landed")

    def test_head_move_blocks_unperformed_update_but_not_completed_update(self):
        linked, child_root, child, _request, _attempt, _descriptor = self.verified()
        original_metadata = copy.deepcopy(self.metadata)
        moved = copy.deepcopy(self.metadata)
        moved["head"]["sha"] = "d" * 40
        self.metadata = moved

        with self.assertRaises(TargetMoved):
            bridge.land(
                child_root,
                self.source_root,
                self.repo,
                attempt=1,
                message="fix: pin the value",
                api=self.api,
            )

        prepared = child.implementation_attempt(1, 1)
        self.assertEqual(prepared["state"], "prepared")
        self.assertEqual(self.branch_head(linked["branch"]), self.head)
        self.metadata = original_metadata
        calls = 0

        def move_after_update(_repo, _number):
            nonlocal calls
            calls += 1
            return copy.deepcopy(original_metadata if calls == 1 else moved)

        landed = bridge.land(
            child_root,
            self.source_root,
            self.repo,
            attempt=1,
            message="fix: pin the value",
            api=move_after_update,
        )

        self.assertTrue(landed["replacement_required"])
        self.assertIn("head_sha changed", landed["replacement_reason"])
        self.assertEqual(child.implementation_attempt(1, 1)["state"], "landed")
        self.assertEqual(self.branch_head(linked["branch"]), landed["artifact"])

    def test_live_pr_timeout_before_ref_update_leaves_the_branch_unchanged(self):
        linked, child_root, child, _request, _attempt, _descriptor = self.verified()
        timeouts = []

        def timeout(_repo, _number, timeout=None):
            timeouts.append(timeout)
            raise SnapshotError("gh timed out")

        with mock.patch.object(bridge, "load_pr", side_effect=timeout):
            with self.assertRaisesRegex(SnapshotError, "timed out"):
                bridge.land(
                    child_root,
                    self.source_root,
                    self.repo,
                    attempt=1,
                    message="fix: pin the value",
                )

        self.assertEqual(len(timeouts), 1)
        self.assertGreater(timeouts[0], 0)
        self.assertLessEqual(timeouts[0], bridge._LAND_SECONDS)
        self.assertEqual(self.branch_head(linked["branch"]), self.head)
        self.assertEqual(child.implementation_attempt(1, 1)["state"], "prepared")

    def test_live_pr_timeout_after_ref_update_preserves_the_landed_receipt(self):
        linked, child_root, child, _request, _attempt, _descriptor = self.verified()
        timeouts = []

        def timeout_after_update(_repo, _number, timeout=None):
            timeouts.append(timeout)
            if len(timeouts) == 1:
                return copy.deepcopy(self.metadata)
            raise SnapshotError("gh timed out")

        with mock.patch.object(
            bridge,
            "load_pr",
            side_effect=timeout_after_update,
        ):
            with self.assertRaisesRegex(SnapshotError, "timed out"):
                bridge.land(
                    child_root,
                    self.source_root,
                    self.repo,
                    attempt=1,
                    message="fix: pin the value",
                )

        self.assertEqual(len(timeouts), 2)
        self.assertTrue(all(0 < timeout <= bridge._LAND_SECONDS for timeout in timeouts))
        landed = child.implementation_attempt(1, 1)
        self.assertEqual(landed["state"], "landed")
        self.assertEqual(
            self.branch_head(linked["branch"]),
            landed["commit_plan"]["commit"],
        )

    def test_moved_head_prevents_link_and_request_side_effects(self):
        moved = copy.deepcopy(self.metadata)
        moved["head"]["sha"] = "e" * 40
        self.metadata = moved
        with self.assertRaises(TargetMoved):
            self.link()
        self.assertFalse((self.source_root / "implementations").exists())

        self.metadata["head"]["sha"] = self.head
        linked, child_root, child = self.linked()
        self.metadata["head"]["sha"] = "e" * 40
        with self.assertRaises(TargetMoved):
            bridge.request(
                child_root,
                self.source_root,
                self.profile,
                api=self.api,
            )
        with self.assertRaises(Exception):
            child.implementation_attempt(1)
        self.assertEqual(self.branch_head(linked["branch"]), self.head)


if __name__ == "__main__":
    unittest.main()
