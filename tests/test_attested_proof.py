#!/usr/bin/env python3
"""Contracts for walk claims run on the frozen PR head through signed gateway evidence."""

import copy
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "underwrite" / "scripts"
for path in (ROOT, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import attested_proof  # noqa: E402
import execution_receipt  # noqa: E402
from gateway import artifacts  # noqa: E402
from gateway.signing import OpenSSLSigner  # noqa: E402
from pr_snapshot import capture  # noqa: E402
from session_store import Conflict, SessionStore, StoreError  # noqa: E402

_spec = importlib.util.spec_from_file_location("render_report", SCRIPTS / "render-report.py")
render_report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(render_report)


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


FAILING = b"F\n======\nFAIL: test_value (tests.test_app.AppTest)\nAssertionError: 1 != 2\n"


class AttestedProofCase(unittest.TestCase):
    merged = False

    def setUp(self):
        if shutil.which("openssl") is None:
            self.skipTest("OpenSSL is required")
        self.temporary = tempfile.TemporaryDirectory(prefix="underwrite-proof-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.remote = self.root / "upstream.git"
        self.controller = self.root / "controller"
        self.session_root = self.root / "session"
        self.git("init", "--bare", "--template=", self.remote)
        self.git("init", "--template=", self.controller)
        for name, value in (
            ("user.name", "Underwrite Test"),
            ("user.email", "underwrite@example.test"),
            ("commit.gpgsign", "false"),
        ):
            self.git("config", name, value, cwd=self.controller)
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
                "repo": {"full_name": "acme/widget", "clone_url": str(self.remote)},
            },
            "head": {
                "ref": "feature",
                "sha": self.head,
                "repo": {"id": 123, "full_name": "acme/widget"},
            },
        }
        if self.merged:
            self.metadata["state"] = "closed"
            self.metadata["merged_at"] = "2026-08-30T12:00:00Z"
        self.store = SessionStore(self.session_root, create=True)
        capture(
            self.store,
            "acme/widget",
            7,
            self.controller,
            api=lambda _repo, _number: copy.deepcopy(self.metadata),
        )
        self.store.patch_session({"repo": "acme/widget", "cursor": 1})

        self.private_key = self.root / "private.pem"
        self.public_key = self.root / "public.pem"
        self.command(
            "openssl", "genpkey", "-algorithm", "EC",
            "-pkeyopt", "ec_paramgen_curve:P-256",
            "-pkeyopt", "ec_param_enc:named_curve",
            "-out", self.private_key,
        )
        self.private_key.chmod(0o600)
        self.public_key.write_bytes(
            self.command("openssl", "pkey", "-in", self.private_key, "-pubout", stdout=True)
        )
        self.signer = OpenSSLSigner(
            self.private_key, self.public_key, "urn:underwrite:signer:test"
        )
        self.workspace_limit = 4 * 1024 * 1024
        self.profile = {
            "version": 1,
            "keyId": self.signer.key_id,
            "signerId": "urn:underwrite:signer:test",
            "executorId": "urn:underwrite:executor:test",
            "job": {
                "argv": ["/usr/bin/python3", "-I", "-B", "-m", "unittest"],
                "cwd": ".",
                "environment": {"LANG": "C"},
                "executable": {"path": "/usr/bin/python3", "sha256": "4" * 64, "bytes": 1},
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
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
        return completed.stdout if stdout else completed.stdout.decode().strip()

    def git(self, *arguments, cwd=None):
        return self.command("git", *arguments, cwd=cwd)

    def reserve(self, beat=1, arguments=("tests.test_app",), exit_code=1):
        return attested_proof.request(
            self.session_root, beat, self.profile, list(arguments), exit_code
        )

    def evidence(self, request_value, *, stdout=b"", stderr=FAILING, signed_exit=None,
                 name=None):
        """The six files a gateway hands back, signed the way the broker signs them."""
        temporary = tempfile.TemporaryDirectory(prefix="underwrite-proof-ws-")
        self.addCleanup(temporary.cleanup)
        workspace = Path(temporary.name) / "workspace"
        source = artifacts.verify_source_bundle(
            self.store.read_object_bundle(),
            self.store.frozen_target(),
            workspace,
            maximum_workspace_bytes=self.workspace_limit,
        )
        built = artifacts.build_output_bundle(
            workspace,
            maximum_workspace_bytes=self.workspace_limit,
            maximum_bundle_bytes=self.workspace_limit * 2,
        )
        expected = {
            "signerId": self.profile["signerId"],
            "executorId": self.profile["executorId"],
            "sessionId": request_value["sessionId"],
            "challenge": request_value["challenge"],
            "target": copy.deepcopy(request_value["target"]),
            "action": copy.deepcopy(request_value["action"]),
            "job": copy.deepcopy(request_value["job"]),
            "inputTree": source.git_tree,
            "sandbox": copy.deepcopy(self.profile["sandbox"]),
            "outputTree": built.descriptor.git_tree,
            "outputBundle": {
                "sha256": built.descriptor.sha256,
                "bytes": built.descriptor.bytes,
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
            "exitCode": request_value["exitCode"] if signed_exit is None else signed_exit,
        }
        capability_payload = execution_receipt.build_host_capability_payload(
            {
                key: expected[key]
                for key in (
                    "signerId", "executorId", "sessionId", "challenge", "target",
                    "action", "job", "inputTree", "sandbox",
                )
            },
            self.now - timedelta(seconds=5),
            self.now + timedelta(minutes=4),
        )
        receipt_payload = execution_receipt.build_execution_receipt_payload(
            capability_payload,
            expected,
            self.now - timedelta(seconds=4),
            self.now - timedelta(seconds=3),
        )
        directory = self.root / (name or f"evidence-{request_value['action']['seq']}")
        directory.mkdir()
        values = {
            "request.json": canonical(request_value),
            "capability.dsse.json": execution_receipt.build_dsse_envelope(
                capability_payload, self.signer.key_id, self.signer.sign
            ),
            "receipt.dsse.json": execution_receipt.build_dsse_envelope(
                receipt_payload, self.signer.key_id, self.signer.sign
            ),
            "output.bundle": built.bundle,
            "stdout": stdout,
            "stderr": stderr,
        }
        for file_name, data in values.items():
            (directory / file_name).write_bytes(data)
        return directory

    def consume(self, proof, directory):
        return attested_proof.consume(
            self.session_root, proof, self.profile, self.public_key, directory, now=self.now
        )

    def verified(self, beat=1, arguments=("tests.test_app",), exit_code=1, **evidence):
        attempt = self.reserve(beat, arguments, exit_code)
        return self.consume(attempt["proof"], self.evidence(attempt["request"], **evidence))

    def attested_beat(self, n=1, proof=1, state="clean", **extra):
        beat = {
            "n": n,
            "tier": "core",
            "state": state,
            "proof_kind": "attested",
            "attested_proof": proof,
            "claim": "the new value fails the existing test",
            "where": "app.py:1",
            "slots": {
                "what": "VALUE moves from 0 to 1",
                "proof": "`python3 -m unittest tests.test_app` exits 1",
            },
        }
        beat.update(extra)
        return beat


class ReservingAProof(AttestedProofCase):
    def test_the_request_is_the_profile_command_with_the_walk_arguments_after_it(self):
        attempt = self.reserve(exit_code=1)

        job = attempt["request"]["job"]
        self.assertEqual(
            job["argv"], self.profile["job"]["argv"] + ["tests.test_app"]
        )
        for name in ("cwd", "environment", "executable", "stdin"):
            self.assertEqual(job[name], self.profile["job"][name])
        self.assertEqual(attempt["request"]["exitCode"], 1)
        self.assertEqual(attempt["request"]["sandbox"], self.profile["sandbox"])
        self.assertEqual(attempt["request"]["target"], self.store.frozen_target())
        self.assertEqual(attempt["request"]["action"], {"seq": 1, "beat": 1, "attempt": 1})
        self.assertEqual(attempt["state"], "reserved")
        self.assertNotIn("capability", attempt)

    def test_a_retry_returns_the_same_reservation_and_a_new_command_gets_its_own(self):
        first = self.reserve()
        again = self.reserve()
        other = self.reserve(exit_code=0)
        other_beat = self.reserve(beat=2)

        self.assertEqual(first, again)
        self.assertEqual(other["proof"], 2)
        self.assertEqual(other_beat["proof"], 3)
        self.assertNotEqual(first["challenge"], other["challenge"])

    def test_only_a_definite_failure_makes_room_for_a_fresh_attempt(self):
        first = self.reserve()
        failed = attested_proof.fail(
            self.session_root, first["proof"], "sandbox exit code was not the trusted expected result"
        )
        retried = self.reserve()

        self.assertEqual(failed["state"], "failed")
        self.assertEqual(retried["proof"], 2)
        self.assertNotEqual(retried["challenge"], first["challenge"])
        with self.assertRaisesRegex(Conflict, "different reason"):
            attested_proof.fail(self.session_root, first["proof"], "something else")

    def test_arguments_and_exit_codes_are_bounded(self):
        cases = {
            "none": ([], 1),
            "too many": (["x"] * 17, 1),
            "empty": ([""], 1),
            "control": (["a\nb"], 1),
            "long": (["a" * 513], 1),
            "exit high": (["x"], 256),
            "exit negative": (["x"], -1),
        }
        for label, (arguments, exit_code) in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(StoreError):
                    attested_proof.request(
                        self.session_root, 1, self.profile, arguments, exit_code
                    )

    def test_a_session_without_a_frozen_pr_runs_no_proofs(self):
        branch = SessionStore(self.root / "branch-session", create=True)
        branch.put_session({"repo": "acme/widget", "audience": {"mode": "branch", "why": "local"}})

        with self.assertRaisesRegex(Conflict, "frozen PR target"):
            branch.reserve_proof(1, self.profile, ["tests"], 0)


class ConsumingEvidence(AttestedProofCase):
    def test_signed_evidence_verifies_and_keeps_what_the_run_printed(self):
        attempt = self.verified(stdout=b"", stderr=FAILING)

        self.assertEqual(attempt["state"], "verified")
        self.assertEqual(attempt["evidence"]["exitCode"], 1)
        self.assertEqual(
            attempt["evidence"]["receiptSha256"],
            hashlib.sha256(
                (self.root / "evidence-1" / "receipt.dsse.json").read_bytes()
            ).hexdigest(),
        )
        self.assertIn("AssertionError: 1 != 2", attempt["output"]["stderr"])
        persisted = self.session_root / "proof-evidence" / "1"
        self.assertEqual(
            sorted(path.name for path in persisted.iterdir()),
            ["capability.dsse.json", "receipt.dsse.json", "request.json", "stderr", "stdout"],
        )

    def test_an_identical_replay_is_accepted_and_changed_bytes_are_not(self):
        attempt = self.reserve()
        directory = self.evidence(attempt["request"])
        first = self.consume(attempt["proof"], directory)
        self.assertEqual(self.consume(attempt["proof"], directory), first)

        (directory / "stderr").write_bytes(FAILING + b"tampered\n")
        with self.assertRaisesRegex(Conflict, "stderr"):
            self.consume(attempt["proof"], directory)

    def test_a_receipt_for_another_exit_code_does_not_verify(self):
        attempt = self.reserve(exit_code=1)
        directory = self.evidence(attempt["request"], signed_exit=0)

        with self.assertRaises(execution_receipt.ReceiptError):
            self.consume(attempt["proof"], directory)
        self.assertEqual(self.store.proof_attempt(attempt["proof"])["state"], "reserved")

    def test_evidence_must_be_for_the_named_proof_and_the_pinned_key(self):
        first = self.reserve()
        second = self.reserve(exit_code=0)
        directory = self.evidence(second["request"])
        with self.assertRaisesRegex(Conflict, "not proof 1"):
            self.consume(first["proof"], directory)

        other_private = self.root / "other.pem"
        other_public = self.root / "other-public.pem"
        self.command(
            "openssl", "genpkey", "-algorithm", "EC",
            "-pkeyopt", "ec_paramgen_curve:P-256",
            "-pkeyopt", "ec_param_enc:named_curve",
            "-out", other_private,
        )
        other_public.write_bytes(
            self.command("openssl", "pkey", "-in", other_private, "-pubout", stdout=True)
        )
        with self.assertRaisesRegex(Conflict, "key ID"):
            attested_proof.consume(
                self.session_root, second["proof"], self.profile, other_public,
                directory, now=self.now,
            )

    def test_a_changed_profile_after_reservation_is_refused(self):
        attempt = self.reserve()
        directory = self.evidence(attempt["request"])
        changed = copy.deepcopy(self.profile)
        changed["sandbox"]["limits"]["memoryBytes"] = 128 * 1024 * 1024

        with self.assertRaisesRegex(Conflict, "profile changed"):
            attested_proof.consume(
                self.session_root, attempt["proof"], changed, self.public_key,
                directory, now=self.now,
            )


class AttestedBeats(AttestedProofCase):
    def test_a_verified_proof_earns_clean_and_the_store_writes_what_ran(self):
        self.verified()

        stored = self.store.put_beat(self.attested_beat())

        self.assertEqual(stored["state"], "clean")
        attestation = stored["attestation"]
        self.assertEqual(attestation["proof"], 1)
        self.assertEqual(attestation["argv"], self.profile["job"]["argv"] + ["tests.test_app"])
        self.assertEqual(attestation["exit_code"], 1)
        self.assertEqual(attestation["key_id"], self.signer.key_id)
        self.assertIn("AssertionError", attestation["output"]["stderr"])
        self.assertEqual(
            self.store.put_beat(copy.deepcopy(stored))["attestation"], attestation
        )

    def test_an_attested_flag_carries_its_receipt_too(self):
        self.verified()

        stored = self.store.put_beat(self.attested_beat(
            state="flag",
            slots={
                "what": "VALUE moves from 0 to 1",
                "proof": "`python3 -m unittest tests.test_app` exits 1",
                "risk": "the shipped default breaks every caller",
                "fix": "keep VALUE at 0",
            },
            decision={
                "question": "Include the failing test in the review, or drop it?",
                "recommendation": "Include it: the gateway ran the test and it failed.",
            },
        ))

        self.assertEqual(stored["state"], "flag")
        self.assertEqual(stored["attestation"]["exit_code"], 1)

    def test_no_verified_receipt_no_attested_proof(self):
        self.reserve()
        with self.assertRaisesRegex(Conflict, "reserved, not verified"):
            self.store.put_beat(self.attested_beat())
        with self.assertRaisesRegex(StoreError, "no proof 9"):
            self.store.put_beat(self.attested_beat(proof=9))
        with self.assertRaisesRegex(StoreError, "names no attested_proof"):
            beat = self.attested_beat()
            del beat["attested_proof"]
            self.store.put_beat(beat)

    def test_a_proof_backs_only_the_beat_it_was_run_for(self):
        self.verified(beat=2)

        with self.assertRaisesRegex(Conflict, "run for beat 2"):
            self.store.put_beat(self.attested_beat(n=1, proof=1))

    def test_the_walk_cannot_write_the_attestation_itself(self):
        self.verified()
        beat = self.attested_beat()
        beat["attestation"] = {"proof": 1, "exit_code": 0}
        with self.assertRaisesRegex(StoreError, "store-owned field attestation"):
            self.store.put_beat(beat)

    def test_a_proof_number_on_another_proof_kind_is_refused(self):
        self.verified()
        beat = self.attested_beat(state="unverified", proof_kind="read")

        with self.assertRaisesRegex(StoreError, "attested_proof is set"):
            self.store.put_beat(beat)

    def test_a_branch_walk_writes_ran_not_attested(self):
        branch = SessionStore(self.root / "branch-session", create=True)
        branch.put_session({"repo": "acme/widget", "audience": {"mode": "branch", "why": "local"}})

        with self.assertRaisesRegex(Conflict, "frozen PR target"):
            branch.put_beat(self.attested_beat())

    def test_the_page_shows_the_signed_command_exit_and_output(self):
        self.verified()
        self.store.put_beat(self.attested_beat())
        session, beats = self.store.presentation_snapshot()[:2]
        beat = beats[0]

        card = render_report.beat_html(beat, render_report.validate(beat), True)
        self.assertIn("Ran in sandbox", card)
        self.assertIn("exited 1, as the claim predicted", card)
        self.assertIn("/usr/bin/python3 -I -B -m unittest tests.test_app", card)
        self.assertIn("AssertionError: 1 != 2", card)
        self.assertNotIn("unproven", card)
        _label, _text, limits, _owed = render_report.assessment(session, beats, {})
        self.assertIn(
            "1 item ran in the gateway sandbox with a signed receipt; "
            "everything else is static inspection.",
            limits,
        )

    def test_an_attested_beat_without_its_attestation_renders_unproven(self):
        beat = self.attested_beat()

        self.assertIn(
            "beat 1: attested proof carries no gateway attestation",
            render_report.validate(beat),
        )


class OneCallProof(AttestedProofCase):
    """`prove` reserves, hands the request to the host adapter, and consumes."""

    def adapter(self, script):
        path = self.root / "adapter.py"
        path.write_text(textwrap.dedent(script), encoding="utf-8")
        outbox = self.root / "outbox"
        outbox.mkdir(exist_ok=True)
        config = self.root / "proof.json"
        profile = self.root / "profile.json"
        profile.write_bytes(canonical(self.profile))
        config.write_bytes(canonical({
            "version": 1,
            "profile": str(profile),
            "publicKey": str(self.public_key),
            "adapter": [sys.executable, str(path)],
            "evidenceRoot": str(outbox),
        }))
        return config

    def test_a_complete_adapter_run_verifies_the_proof(self):
        attempt = self.reserve()
        prepared = self.evidence(attempt["request"], name="prepared")
        config = self.adapter(f"""
            import json, shutil, sys
            args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
            shutil.copytree({str(prepared)!r}, args["--evidence-dir"])
            print(json.dumps({{"version": 1, "status": "complete",
                               "evidenceDir": args["--evidence-dir"]}}))
        """)

        result = attested_proof.prove(
            self.session_root, 1, config, ["tests.test_app"], 1, now=self.now
        )

        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["proof"]["proof"], attempt["proof"])
        self.assertEqual(self.store.proof_attempt(1)["state"], "verified")

    def test_a_definite_gateway_failure_records_why(self):
        config = self.adapter("""
            import json, sys
            print(json.dumps({"version": 1, "status": "failed",
                              "reason": "gateway attempt is terminal without a receipt"}))
            print("adapter.py: sandbox exit code was not the trusted expected result",
                  file=sys.stderr)
            sys.exit(2)
        """)

        result = attested_proof.prove(self.session_root, 1, config, ["tests.test_app"], 0)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["proof"]["failure"],
            "adapter.py: sandbox exit code was not the trusted expected result",
        )
        self.assertEqual(self.store.proof_attempt(1)["state"], "failed")

    def test_an_unknown_outcome_keeps_the_reservation_for_a_retry(self):
        config = self.adapter("""
            import sys
            print("not json")
            sys.exit(1)
        """)

        result = attested_proof.prove(self.session_root, 1, config, ["tests.test_app"], 0)

        self.assertEqual(result["status"], "retry")
        self.assertEqual(self.store.proof_attempt(1)["state"], "reserved")

    def test_the_configuration_has_one_exact_shape(self):
        config = self.adapter("print('unused')")
        value = json.loads(config.read_text())
        value["adapter"] = ["relative/adapter.py"]
        config.write_bytes(canonical(value))

        with self.assertRaisesRegex(attested_proof.ProofError, "absolute"):
            attested_proof.prove(self.session_root, 1, config, ["tests.test_app"], 0)


class AttestedReportFindings(AttestedProofCase):
    merged = True

    def test_an_included_finding_keeps_the_proof_it_was_included_on(self):
        self.verified()
        self.verified(exit_code=0, stderr=b"OK\n", name="evidence-second")
        self.store.put_beat(self.attested_beat(
            state="flag",
            slots={
                "what": "VALUE moves from 0 to 1",
                "proof": "`python3 -m unittest tests.test_app` exits 1",
                "risk": "the shipped default breaks every caller",
                "fix": "keep VALUE at 0",
            },
            decision={
                "question": "Include the failing test in the report, or drop it?",
                "recommendation": "Include it: the gateway ran the test and it failed.",
            },
        ))
        self.assertEqual(self.store.snapshot()[0]["audience"]["mode"], "report")
        accepted = self.store.produce("include-1", 1, "accept", "include it")
        self.store.ack(accepted["seq"])
        current = self.store.snapshot()[1][0]
        self.assertEqual(current["state"], "accepted")

        swapped = copy.deepcopy(current)
        swapped["attested_proof"] = 2
        with self.assertRaisesRegex(Conflict, "cannot change attested_proof"):
            self.store.put_beat(swapped)
        self.assertEqual(self.store.put_beat(current)["attestation"]["proof"], 1)


class Upgrading(AttestedProofCase):
    def test_a_version_7_session_gains_the_proof_table_and_keeps_its_state(self):
        import sqlite3

        before = self.store.snapshot()
        db = sqlite3.connect(str(self.store.path))
        try:
            db.execute("DROP TABLE proof_attempts")
            db.execute("PRAGMA user_version = 7")
            db.commit()
        finally:
            db.close()

        reopened = SessionStore(self.session_root)

        self.assertEqual(reopened.snapshot(), before)
        self.assertEqual(reopened.proof_attempts(), [])
        db = sqlite3.connect(str(self.store.path))
        try:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 8)
        finally:
            db.close()


class CommandLine(AttestedProofCase):
    def proofctl(self, *arguments):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "proofctl.py"), *map(str, arguments)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_request_takes_dashed_arguments_after_a_separator(self):
        profile = self.root / "profile.json"
        profile.write_bytes(canonical(self.profile))

        completed = self.proofctl(
            "request", self.session_root, 1, profile, "--exit-code", "1",
            "--", "tests.test_app", "-k", "value",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        attempt = json.loads(completed.stdout)
        self.assertEqual(
            attempt["request"]["job"]["argv"][-3:], ["tests.test_app", "-k", "value"]
        )
        listed = json.loads(self.proofctl("proofs", self.session_root).stdout)
        self.assertEqual([item["proof"] for item in listed["proofs"]], [1])

    def test_a_refusal_exits_one_with_the_reason(self):
        profile = self.root / "profile.json"
        profile.write_bytes(canonical(self.profile))

        completed = self.proofctl(
            "request", self.session_root, 1, profile, "--exit-code", "300", "tests"
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("proofctl: proof exit code must not exceed 255", completed.stderr.decode())


if __name__ == "__main__":
    unittest.main()
