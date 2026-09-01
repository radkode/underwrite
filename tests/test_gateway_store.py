#!/usr/bin/env python3
"""Host storage, replay, and asymmetric signing boundary tests."""

import concurrent.futures
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from gateway.signing import OpenSSLSigner, OpenSSLVerifier, SigningError
from gateway.store import (
    ArtifactRef,
    ContentStore,
    ReplayConflict,
    ReplayLedger,
    StoreError,
    StoredExecution,
)


def object_path(store, reference):
    return store.objects / reference.sha256[:2] / reference.sha256[2:]


class ContentStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.store = ContentStore(self.base / "store")

    def tearDown(self):
        self.temporary.cleanup()

    def test_content_addressing_deduplicates_bytes_and_files(self):
        payload = b"immutable execution artifact\n"
        source = self.base / "artifact.bin"
        source.write_bytes(payload)

        from_bytes = self.store.put_bytes(payload)
        from_file = self.store.put_file(source, maximum_bytes=len(payload))

        self.assertEqual(from_bytes, from_file)
        self.assertEqual(
            from_bytes,
            ArtifactRef(hashlib.sha256(payload).hexdigest(), len(payload)),
        )
        self.assertEqual(self.store.read(from_bytes), payload)
        stored = object_path(self.store, from_bytes)
        self.assertEqual(stat.S_IMODE(stored.stat().st_mode), 0o600)
        self.assertEqual(len(list(stored.parent.iterdir())), 1)

    def test_same_size_object_mutation_is_rejected(self):
        reference = self.store.put_bytes(b"first artifact")
        object_path(self.store, reference).write_bytes(b"other artifact")
        self.assertEqual(reference.bytes, len(b"other artifact"))

        with self.assertRaisesRegex(StoreError, "does not match"):
            self.store.read(reference)

    def test_invalid_reference_cannot_escape_the_object_store(self):
        with self.assertRaisesRegex(StoreError, "reference is malformed"):
            self.store.read(ArtifactRef("../../etc/passwd", 1))

    def test_private_directories_and_database_are_enforced(self):
        ledger = ReplayLedger(self.store)
        for path in (
            self.store.root,
            self.store.objects,
            self.store.staging,
        ):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(ledger.path.stat().st_mode), 0o600)

        exposed = self.base / "exposed"
        exposed.mkdir(mode=0o700)
        exposed.chmod(0o755)
        with self.assertRaisesRegex(StoreError, "not private"):
            ContentStore(exposed)

    def test_preexisting_public_object_directory_is_rejected(self):
        payload = b"private object"
        digest = hashlib.sha256(payload).hexdigest()
        prefix = self.store.objects / digest[:2]
        prefix.mkdir()
        prefix.chmod(0o755)

        with self.assertRaisesRegex(StoreError, "not private"):
            self.store.put_bytes(payload)
        self.assertEqual(list(self.store.staging.iterdir()), [])

    def test_failed_file_copy_removes_its_staging_file(self):
        source = self.base / "large.bin"
        source.write_bytes(b"too large")

        with self.assertRaisesRegex(StoreError, "exceeds"):
            self.store.put_file(source, maximum_bytes=1)
        self.assertEqual(list(self.store.staging.iterdir()), [])

        with self.assertRaisesRegex(StoreError, "cannot be opened"):
            self.store.put_file(self.base / "missing.bin")
        self.assertEqual(list(self.store.staging.iterdir()), [])

    def test_file_copy_rejects_invalid_limits_even_for_an_empty_file(self):
        source = self.base / "empty.bin"
        source.write_bytes(b"")
        for limit in (-1, True, 1.5):
            with self.subTest(limit=limit):
                with self.assertRaisesRegex(StoreError, "byte limit"):
                    self.store.put_file(source, maximum_bytes=limit)

    def test_bounded_file_load_is_exact_and_rejects_symbolic_links(self):
        payload = b"frozen source bundle"
        source = self.base / "source.bundle"
        source.write_bytes(payload)

        self.assertEqual(self.store.load_file(source, len(payload)), payload)
        with self.assertRaisesRegex(StoreError, "exceeds"):
            self.store.load_file(source, len(payload) - 1)

        link = self.base / "source-link.bundle"
        link.symlink_to(source)
        with self.assertRaisesRegex(StoreError, "cannot be opened"):
            self.store.load_file(link, len(payload))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO support is required")
    def test_bounded_file_load_rejects_a_fifo_without_blocking(self):
        source = self.base / "source.fifo"
        os.mkfifo(source)

        with self.assertRaisesRegex(StoreError, "regular file"):
            self.store.load_file(source, 1024)


class ReplayLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.content = ContentStore(self.base / "store")
        self.ledger = ReplayLedger(self.content)
        self.source = self.content.put_bytes(b"frozen source bundle")
        self.request = b'{"action":{"seq":1},"target":"frozen"}'

    def tearDown(self):
        self.temporary.cleanup()

    def complete_attempt(
        self,
        replay_key="replay-1",
        challenge_key="challenge-1",
        request=None,
        source=None,
    ):
        request = self.request if request is None else request
        source = self.source if source is None else source
        self.assertIsNone(
            self.ledger.reserve(replay_key, challenge_key, request, source)
        )
        self.ledger.record_capability(replay_key, b"signed capability")
        self.ledger.begin_execution(replay_key)
        output = self.content.put_bytes(("output:" + replay_key).encode("utf-8"))
        result = self.ledger.complete(
            replay_key,
            b"signed receipt",
            {"output.bundle": output},
            b'{"verified":true}',
        )
        return result, output

    def update_attempt(self, assignment, values, replay_key="replay-1"):
        with sqlite3.connect(str(self.ledger.path)) as database:
            database.execute(
                f"UPDATE attempts SET {assignment} WHERE replay_key = ?",
                tuple(values) + (replay_key,),
            )

    def test_concurrent_reservation_has_exactly_one_winner(self):
        workers = 8
        barrier = threading.Barrier(workers)

        def reserve():
            barrier.wait()
            try:
                result = self.ledger.reserve(
                    "shared-replay", "shared-challenge", self.request, self.source
                )
            except ReplayConflict:
                return "conflict"
            self.assertIsNone(result)
            return "reserved"

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = list(pool.map(lambda _index: reserve(), range(workers)))

        self.assertEqual(outcomes.count("reserved"), 1)
        self.assertEqual(outcomes.count("conflict"), workers - 1)
        with sqlite3.connect(str(self.ledger.path)) as database:
            count = database.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
        self.assertEqual(count, 1)

    def test_completed_replay_requires_every_reserved_identity_input(self):
        expected, _output = self.complete_attempt()
        replay = self.ledger.reserve(
            "replay-1", "challenge-1", self.request, self.source
        )
        self.assertEqual(replay, expected)

        other_source = self.content.put_bytes(b"different frozen source")
        conflicts = (
            ("replay-1", "different-challenge", self.request, self.source),
            ("different-replay", "challenge-1", self.request, self.source),
            ("replay-1", "challenge-1", self.request + b" ", self.source),
            ("replay-1", "challenge-1", self.request, other_source),
        )
        for replay_key, challenge_key, request, source in conflicts:
            with self.subTest(
                replay_key=replay_key,
                challenge_key=challenge_key,
                request=request,
                source=source,
            ):
                with self.assertRaises(ReplayConflict):
                    self.ledger.reserve(
                        replay_key, challenge_key, request, source
                    )

    def test_lookup_complete_short_circuits_source_and_signing_work(self):
        expected, output = self.complete_attempt()
        original_read = self.content.read
        signer = mock.Mock(side_effect=AssertionError("signer must not be called"))

        def reject_source_read(reference):
            if reference == self.source:
                raise AssertionError("source must not be re-verified")
            return original_read(reference)

        with mock.patch.object(self.content, "read", side_effect=reject_source_read):
            replay = self.ledger.lookup_complete(
                "replay-1", self.request, self.source
            )
            if replay is None:
                signer()

        self.assertEqual(replay, expected)
        self.assertEqual(replay.artifact("output.bundle"), output)
        signer.assert_not_called()
        self.assertIsNone(
            self.ledger.lookup_complete("absent", self.request, self.source)
        )
        with self.assertRaises(ReplayConflict):
            self.ledger.lookup_complete(
                "replay-1", self.request + b" ", self.source
            )

    def test_transition_order_is_fail_closed(self):
        self.assertIsNone(
            self.ledger.reserve(
                "ordered", "ordered-challenge", self.request, self.source
            )
        )
        artifact = self.content.put_bytes(b"output")

        with self.assertRaises(ReplayConflict):
            self.ledger.begin_execution("ordered")
        with self.assertRaises(ReplayConflict):
            self.ledger.complete(
                "ordered", b"receipt", {"output": artifact}, b"validation"
            )
        self.ledger.record_capability("ordered", b"capability")
        with self.assertRaises(ReplayConflict):
            self.ledger.record_capability("ordered", b"second capability")
        with self.assertRaises(ReplayConflict):
            self.ledger.complete(
                "ordered", b"receipt", {"output": artifact}, b"validation"
            )
        self.ledger.begin_execution("ordered")
        with self.assertRaises(ReplayConflict):
            self.ledger.begin_execution("ordered")
        completed = self.ledger.complete(
            "ordered", b"receipt", {"output": artifact}, b"validation"
        )
        self.assertIsInstance(completed, StoredExecution)
        with self.assertRaises(ReplayConflict):
            self.ledger.fail("ordered", "too late")

    def test_signer_failure_can_terminate_a_preparing_reservation(self):
        self.assertIsNone(
            self.ledger.reserve(
                "signer-failed", "signer-challenge", self.request, self.source
            )
        )
        self.ledger.fail("signer-failed", "host signer unavailable")

        with sqlite3.connect(str(self.ledger.path)) as database:
            state, capability, reason = database.execute(
                "SELECT state, capability, failure FROM attempts WHERE replay_key = ?",
                ("signer-failed",),
            ).fetchone()
        self.assertEqual((state, capability, reason), (
            "failed",
            None,
            "host signer unavailable",
        ))
        with self.assertRaisesRegex(ReplayConflict, "failed"):
            self.ledger.lookup_complete(
                "signer-failed", self.request, self.source
            )
        with self.assertRaisesRegex(ReplayConflict, "failed"):
            self.ledger.reserve(
                "signer-failed", "signer-challenge", self.request, self.source
            )
        with self.assertRaisesRegex(ReplayConflict, "already terminal"):
            self.ledger.fail("signer-failed", "retry")

    def test_nonterminal_attempts_remain_ambiguous_across_restarts(self):
        states = ("preparing", "ready", "executing")
        for index, state in enumerate(states):
            replay_key = f"ambiguous-{state}"
            challenge_key = f"challenge-{index}"
            self.ledger.reserve(
                replay_key, challenge_key, self.request, self.source
            )
            if state in ("ready", "executing"):
                self.ledger.record_capability(replay_key, b"capability")
            if state == "executing":
                self.ledger.begin_execution(replay_key)

            restarted = ReplayLedger(self.content)
            with self.subTest(state=state):
                with self.assertRaisesRegex(ReplayConflict, state) as lookup:
                    restarted.lookup_complete(
                        replay_key, self.request, self.source
                    )
                with self.assertRaisesRegex(ReplayConflict, state) as reserve:
                    restarted.reserve(
                        replay_key, challenge_key, self.request, self.source
                    )
                self.assertEqual(lookup.exception.state, state)
                self.assertEqual(reserve.exception.state, state)

    def test_completed_record_detects_request_and_capability_tampering(self):
        self.complete_attempt()
        self.update_attempt("capability = ?", (b"signed capabilitx",))
        with self.assertRaisesRegex(StoreError, "capability"):
            self.ledger.get("replay-1")

        self.update_attempt("capability = ?", (b"signed capability",))
        replacement = b'X' + self.request[1:]
        self.assertEqual(len(replacement), len(self.request))
        self.update_attempt("request = ?", (replacement,))
        with self.assertRaisesRegex(StoreError, "request"):
            self.ledger.get("replay-1")

    def test_completed_record_detects_receipt_and_validation_tampering(self):
        self.complete_attempt()
        self.update_attempt("receipt = ?", (b"signed receipu",))
        with self.assertRaisesRegex(StoreError, "receipt"):
            self.ledger.get("replay-1")

        self.update_attempt("receipt = ?", (b"signed receipt",))
        self.update_attempt("validation_json = ?", (b'{"verified":falsf}',))
        with self.assertRaisesRegex(StoreError, "validation"):
            self.ledger.get("replay-1")

    def test_artifact_manifest_is_canonical_and_integrity_checked(self):
        result, output = self.complete_attempt()
        self.assertEqual(result.artifacts, (("output.bundle", output),))
        with self.assertRaises(KeyError):
            result.artifact("missing")

        with sqlite3.connect(str(self.ledger.path)) as database:
            raw = database.execute(
                "SELECT artifacts_json FROM attempts WHERE replay_key = ?",
                ("replay-1",),
            ).fetchone()[0]
        expected = json.dumps(
            {"output.bundle": output.as_dict()},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self.assertEqual(raw, expected)

        self.update_attempt("artifacts_json = ?", (b'{ "output.bundle": {}}',))
        with self.assertRaisesRegex(StoreError, "manifest"):
            self.ledger.get("replay-1")

        missing = {
            "output.bundle": {"sha256": "0" * 64, "bytes": output.bytes}
        }
        missing_raw = json.dumps(
            missing, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        self.update_attempt(
            "artifacts_json = ?, artifacts_sha256 = ?",
            (missing_raw, hashlib.sha256(missing_raw).hexdigest()),
        )
        with self.assertRaisesRegex(StoreError, "missing"):
            self.ledger.get("replay-1")

    def test_completed_record_rechecks_same_size_artifact_content(self):
        _result, output = self.complete_attempt()
        path = object_path(self.content, output)
        replacement = b"X" * output.bytes
        self.assertEqual(len(replacement), output.bytes)
        path.write_bytes(replacement)

        with self.assertRaisesRegex(StoreError, "does not match"):
            self.ledger.lookup_complete(
                "replay-1", self.request, self.source
            )


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class OpenSSLSignerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.openssl = shutil.which("openssl")
        self.private_key = self.base / "private.pem"
        self.public_key = self.base / "public.pem"
        self.run_openssl(
            "genpkey",
            "-algorithm",
            "EC",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-pkeyopt",
            "ec_param_enc:named_curve",
            "-out",
            str(self.private_key),
        )
        self.private_key.chmod(0o600)
        self.run_openssl(
            "pkey",
            "-in",
            str(self.private_key),
            "-pubout",
            "-out",
            str(self.public_key),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def run_openssl(self, *arguments):
        return subprocess.run(
            [self.openssl] + list(arguments),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_ephemeral_ecdsa_key_signs_and_verifies_exact_pae_bytes(self):
        signer = OpenSSLSigner(
            self.private_key,
            self.public_key,
            "host:test-signer",
            openssl=self.openssl,
        )
        pae = b"DSSEv1 4 test 7 payload"
        signature = signer.sign(pae, signer.key_id)

        self.assertTrue(signature)
        self.assertRegex(signer.key_id, r"\Asha256:[0-9a-f]{64}\Z")
        self.assertEqual(
            signer.verify(pae, signer.key_id, signature),
            "host:test-signer",
        )
        with self.assertRaisesRegex(SigningError, "verification failed"):
            signer.verify(pae + b"!", signer.key_id, signature)
        with self.assertRaisesRegex(SigningError, "key ID"):
            signer.sign(pae, "sha256:" + "0" * 64)

    def test_public_verifier_never_loads_or_probes_the_private_key(self):
        signer = OpenSSLSigner(
            self.private_key,
            self.public_key,
            "host:test-signer",
            openssl=self.openssl,
        )
        pae = b"DSSEv1 4 test 15 public boundary"
        signature = signer.sign(pae, signer.key_id)
        self.private_key.unlink()

        real_run = subprocess.run
        with mock.patch(
            "gateway.signing.subprocess.run",
            wraps=real_run,
        ) as run:
            verifier = OpenSSLVerifier(
                self.public_key,
                "host:test-signer",
                openssl=self.openssl,
            )

        self.assertEqual(verifier.key_id, signer.key_id)
        self.assertFalse(hasattr(verifier, "private_key"))
        self.assertFalse(
            any("-sign" in call.args[0] for call in run.call_args_list)
        )
        self.assertEqual(
            verifier.verify(pae, verifier.key_id, signature),
            "host:test-signer",
        )
        with self.assertRaisesRegex(SigningError, "key ID"):
            verifier.verify(pae, "sha256:" + "0" * 64, signature)
        with self.assertRaisesRegex(SigningError, "verification failed"):
            verifier.verify(
                pae,
                verifier.key_id,
                signature[:-1] + bytes((signature[-1] ^ 1,)),
            )

    def test_private_key_must_not_be_group_or_world_accessible(self):
        self.private_key.chmod(0o644)
        with self.assertRaisesRegex(SigningError, "group or other"):
            OpenSSLSigner(
                self.private_key,
                self.public_key,
                "host:test-signer",
                openssl=self.openssl,
            )

    def test_signer_rejects_a_non_p256_key_pair(self):
        private_key = self.base / "rsa-private.pem"
        public_key = self.base / "rsa-public.pem"
        self.run_openssl(
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            str(private_key),
        )
        private_key.chmod(0o600)
        self.run_openssl(
            "pkey",
            "-in",
            str(private_key),
            "-pubout",
            "-out",
            str(public_key),
        )

        with self.assertRaisesRegex(SigningError, "P-256"):
            OpenSSLSigner(
                private_key,
                public_key,
                "host:test-signer",
                openssl=self.openssl,
            )
        with self.assertRaisesRegex(SigningError, "P-256"):
            OpenSSLVerifier(
                public_key,
                "host:test-signer",
                openssl=self.openssl,
            )

    def test_signer_pins_key_bytes_before_paths_can_change(self):
        signer = OpenSSLSigner(
            self.private_key,
            self.public_key,
            "host:test-signer",
            openssl=self.openssl,
        )
        self.private_key.write_bytes(b"replaced private key")
        self.public_key.write_bytes(b"replaced public key")
        pae = b"DSSEv1 4 test 6 pinned"

        signature = signer.sign(pae, signer.key_id)

        self.assertEqual(
            signer.verify(pae, signer.key_id, signature),
            "host:test-signer",
        )

    def test_signer_bounds_openssl_processes(self):
        signer = object.__new__(OpenSSLSigner)
        signer.openssl = self.openssl
        timeout = subprocess.TimeoutExpired([self.openssl, "version"], 30)
        with mock.patch(
            "gateway.signing.subprocess.run",
            side_effect=timeout,
        ) as run:
            with self.assertRaisesRegex(SigningError, "timed out"):
                signer._run([self.openssl, "version"])
        self.assertEqual(run.call_args.kwargs["timeout"], 30)


if __name__ == "__main__":
    unittest.main()
