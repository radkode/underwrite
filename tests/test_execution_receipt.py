#!/usr/bin/env python3
"""Adversarial contracts for authenticated host execution receipts."""

import ast
import base64
import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "underwrite" / "scripts"
SPEC = importlib.util.spec_from_file_location(
    "execution_receipt", SCRIPTS / "execution_receipt.py"
)
execution_receipt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(execution_receipt)


PAYLOAD_TYPE = "application/vnd.in-toto+json"
STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
CAPABILITY_TYPE = (
    "https://github.com/radkode/underwrite/attestations/host-capability/v1"
)
RECEIPT_TYPE = (
    "https://github.com/radkode/underwrite/attestations/execution-receipt/v1"
)
SANDBOX_POLICY_TYPE = (
    "https://github.com/radkode/underwrite/sandbox-policy/v1"
)
KEY_ID = "host-key-lookup"
SIGNER_ID = "https://runner.example/hosts/runner-7"
OTHER_SIGNER_ID = "https://runner.example/hosts/runner-8"
SIGNATURE = b"\xfb\xff"
SESSION_ID = "71f9b79c-9b2a-4b89-a69c-f3ed3a032abc"
CHALLENGE = "7" * 64
EXECUTOR_ID = "underwrite-host-runner-7"
OTHER_EXECUTOR_ID = "underwrite-host-runner-8"
INPUT_TREE = "1" * 64
OUTPUT_TREE = "2" * 64
SOURCE_BUNDLE_SHA256 = "3" * 64
OUTPUT_BUNDLE_SHA256 = "4" * 64
STDOUT_SHA256 = "5" * 64
STDERR_SHA256 = "6" * 64
ISSUED_AT = "2026-08-29T11:55:00Z"
STARTED_AT = "2026-08-29T11:59:00Z"
FINISHED_AT = "2026-08-29T11:59:30Z"
EXPIRES_AT = "2026-08-29T12:00:00Z"
NOW = datetime(2026, 8, 29, 11, 59, 45, tzinfo=timezone.utc)


TARGET = {
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
    "head_ref": "feature/receipt",
    "merge_base_sha": "c" * 40,
    "changed_files": 3,
    "diff_sha256": "d" * 64,
    "diff_bytes": 2048,
    "trusted_context_sha256": "e" * 64,
    "trusted_context_bytes": 512,
    "object_bundle_sha256": SOURCE_BUNDLE_SHA256,
    "object_bundle_bytes": 8192,
}
ACTION = {"seq": 41, "beat": 7, "attempt": 2}
JOB = {
    "argv": ["/usr/bin/python3", "-c", "print('café')"],
    "cwd": "source",
    "environment": {"LANG": "C.UTF-8", "TZ": "UTC"},
    "executable": {
        "path": "/usr/bin/python3",
        "sha256": "f" * 64,
        "bytes": 1048576,
    },
    "stdin": "closed",
}
SANDBOX = {
    "policy": SANDBOX_POLICY_TYPE,
    "credentials": "absent",
    "network": "denied",
    "hostWrites": "denied",
    "gitHooks": "disabled",
    "gitFilters": "disabled",
    "timeout": "enforced",
    "limits": {
        "wallSeconds": 60,
        "cpuSeconds": 30,
        "memoryBytes": 536870912,
        "processes": 64,
        "workspaceBytes": 1073741824,
        "outputBytes": 10485760,
    },
}
STDOUT = {"sha256": STDOUT_SHA256, "bytes": 12, "truncated": False}
STDERR = {"sha256": STDERR_SHA256, "bytes": 0, "truncated": False}


def fixture_json_bytes(value, *, pretty=False):
    options = {
        "allow_nan": False,
        "ensure_ascii": False,
        "sort_keys": True,
    }
    if pretty:
        options["indent"] = 2
    else:
        options["separators"] = (",", ":")
    return json.dumps(value, **options).encode("utf-8")


def fixture_sha256(value):
    return hashlib.sha256(fixture_json_bytes(value)).hexdigest()


def fixture_pae(payload_type, payload):
    type_bytes = payload_type.encode("utf-8")
    return b"".join(
        (
            b"DSSEv1 ",
            str(len(type_bytes)).encode("ascii"),
            b" ",
            type_bytes,
            b" ",
            str(len(payload)).encode("ascii"),
            b" ",
            payload,
        )
    )


def fixture_envelope(
    payload,
    *,
    keyid=KEY_ID,
    signature=SIGNATURE,
    payload_type=PAYLOAD_TYPE,
    omit_keyid=False,
):
    signature_entry = {"sig": base64.b64encode(signature).decode("ascii")}
    if not omit_keyid:
        signature_entry["keyid"] = keyid
    return fixture_json_bytes(
        {
            "payloadType": payload_type,
            "payload": base64.b64encode(payload).decode("ascii"),
            "signatures": [signature_entry],
        }
    )


class SignatureRecorder:
    def __init__(self, *signer_ids, error_at=None):
        self.signer_ids = signer_ids or (SIGNER_ID,)
        self.error_at = error_at
        self.calls = []

    def __call__(self, pae_bytes, keyid, signature_bytes):
        call = len(self.calls)
        self.calls.append((pae_bytes, keyid, signature_bytes))
        if self.error_at == call:
            raise RuntimeError("fixture signature rejection")
        return self.signer_ids[min(call, len(self.signer_ids) - 1)]


class ReceiptCase(unittest.TestCase):
    def setUp(self):
        self.target = copy.deepcopy(TARGET)
        self.action = copy.deepcopy(ACTION)
        self.job = copy.deepcopy(JOB)
        self.sandbox = copy.deepcopy(SANDBOX)
        self.capability_statement = self.make_capability_statement()
        self.capability_payload = fixture_json_bytes(self.capability_statement)
        self.capability_envelope = fixture_envelope(self.capability_payload)
        self.receipt_statement = self.make_receipt_statement(self.capability_payload)
        self.receipt_payload = fixture_json_bytes(self.receipt_statement)
        self.receipt_envelope = fixture_envelope(self.receipt_payload)
        self.expected = self.make_expected()

    def invocation(self):
        return {
            "session": {"id": SESSION_ID, "challenge": CHALLENGE},
            "target": {
                "document": copy.deepcopy(self.target),
                "digest": {"sha256": fixture_sha256(self.target)},
            },
            "sourceBundle": {
                "sha256": self.target["object_bundle_sha256"],
                "bytes": self.target["object_bundle_bytes"],
            },
            "action": copy.deepcopy(self.action),
            "job": copy.deepcopy(self.job),
            "jobDigest": {"sha256": fixture_sha256(self.job)},
            "inputTree": {"gitTree": INPUT_TREE},
            "sandbox": copy.deepcopy(self.sandbox),
        }

    def make_capability_statement(self):
        return {
            "_type": STATEMENT_TYPE,
            "subject": [
                {
                    "name": "underwrite-execution-input",
                    "digest": {"gitTree": INPUT_TREE},
                }
            ],
            "predicateType": CAPABILITY_TYPE,
            "predicate": {
                "executor": {"id": EXECUTOR_ID},
                "issuedAt": ISSUED_AT,
                "expiresAt": EXPIRES_AT,
                "invocation": self.invocation(),
            },
        }

    def make_receipt_statement(self, capability_payload):
        return {
            "_type": STATEMENT_TYPE,
            "subject": [
                {
                    "name": "underwrite-execution-output",
                    "digest": {"gitTree": OUTPUT_TREE},
                }
            ],
            "predicateType": RECEIPT_TYPE,
            "predicate": {
                "executor": {"id": EXECUTOR_ID},
                "capability": {
                    "payloadSha256": hashlib.sha256(capability_payload).hexdigest()
                },
                "startedAt": STARTED_AT,
                "finishedAt": FINISHED_AT,
                "invocation": self.invocation(),
                "outputTree": {"gitTree": OUTPUT_TREE},
                "outputBundle": {
                    "gitTree": OUTPUT_TREE,
                    "sha256": OUTPUT_BUNDLE_SHA256,
                    "bytes": 16384,
                },
                "result": {
                    "status": "exited",
                    "exitCode": 0,
                    "signal": None,
                    "timedOut": False,
                    "resourceViolation": None,
                    "isolationViolation": None,
                    "survivingProcesses": 0,
                    "teardown": "complete",
                },
                "streams": {
                    "stdout": copy.deepcopy(STDOUT),
                    "stderr": copy.deepcopy(STDERR),
                },
            },
        }

    def make_expected(self):
        return {
            "signerId": SIGNER_ID,
            "executorId": EXECUTOR_ID,
            "sessionId": SESSION_ID,
            "challenge": CHALLENGE,
            "target": copy.deepcopy(self.target),
            "action": copy.deepcopy(self.action),
            "job": copy.deepcopy(self.job),
            "inputTree": INPUT_TREE,
            "sandbox": copy.deepcopy(self.sandbox),
            "outputTree": OUTPUT_TREE,
            "outputBundle": {
                "sha256": OUTPUT_BUNDLE_SHA256,
                "bytes": 16384,
            },
            "stdout": copy.deepcopy(STDOUT),
            "stderr": copy.deepcopy(STDERR),
            "exitCode": 0,
        }

    def capability_expected(self, **changes):
        expected = {
            key: copy.deepcopy(self.expected[key])
            for key in (
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
        expected.update(changes)
        return expected

    def encode_capability(self, statement=None, *, pretty=False):
        statement = statement or self.capability_statement
        payload = fixture_json_bytes(statement, pretty=pretty)
        return fixture_envelope(payload), payload

    def encode_receipt(self, statement=None, *, pretty=False):
        statement = statement or self.receipt_statement
        payload = fixture_json_bytes(statement, pretty=pretty)
        return fixture_envelope(payload), payload

    def verify_capability(self, envelope=None, expected=None, callback=None, now=NOW):
        callback = callback or SignatureRecorder()
        result = execution_receipt.verify_host_capability(
            envelope or self.capability_envelope,
            expected or self.capability_expected(),
            callback,
            now,
        )
        return result, callback

    def verify_receipt(
        self,
        capability_envelope=None,
        receipt_envelope=None,
        expected=None,
        callback=None,
        now=NOW,
    ):
        callback = callback or SignatureRecorder()
        result = execution_receipt.verify_execution_receipt(
            capability_envelope or self.capability_envelope,
            receipt_envelope or self.receipt_envelope,
            expected or copy.deepcopy(self.expected),
            callback,
            now,
        )
        return result, callback

    def assert_capability_rejected(
        self, envelope=None, expected=None, callback=None, now=NOW
    ):
        callback = callback or SignatureRecorder()
        with self.assertRaises(execution_receipt.ReceiptError):
            execution_receipt.verify_host_capability(
                envelope or self.capability_envelope,
                expected or self.capability_expected(),
                callback,
                now,
            )
        return callback

    def assert_receipt_rejected(
        self,
        capability_envelope=None,
        receipt_envelope=None,
        expected=None,
        callback=None,
        now=NOW,
    ):
        callback = callback or SignatureRecorder()
        with self.assertRaises(execution_receipt.ReceiptError):
            execution_receipt.verify_execution_receipt(
                capability_envelope or self.capability_envelope,
                receipt_envelope or self.receipt_envelope,
                expected or copy.deepcopy(self.expected),
                callback,
                now,
            )
        return callback


class PrimitiveContracts(ReceiptCase):
    def test_pae_is_exact_and_counts_multibyte_payload_bytes(self):
        payload = "café".encode("utf-8")

        self.assertEqual(
            execution_receipt.dsse_pae("text/plain", payload),
            b"DSSEv1 10 text/plain 5 caf\xc3\xa9",
        )

    def test_canonical_sha256_is_independent_of_object_key_order(self):
        first = {"z": "café", "a": [1, True, None]}
        second = {"a": [1, True, None], "z": "café"}
        expected = hashlib.sha256(fixture_json_bytes(first)).hexdigest()

        self.assertEqual(execution_receipt.canonical_sha256(first), expected)
        self.assertEqual(execution_receipt.canonical_sha256(second), expected)
        self.assertNotEqual(
            execution_receipt.canonical_sha256({"argv": ["ab", "c"]}),
            execution_receipt.canonical_sha256({"argv": ["a", "bc"]}),
        )

    def test_valid_capability_returns_the_exact_authenticated_payload(self):
        verified, callback = self.verify_capability()

        self.assertEqual(verified.signer_id, SIGNER_ID)
        self.assertEqual(verified.payload, self.capability_payload)
        self.assertEqual(
            verified.payload_sha256,
            hashlib.sha256(self.capability_payload).hexdigest(),
        )
        self.assertEqual(json.loads(verified.payload), self.capability_statement)
        self.assertEqual(
            callback.calls,
            [(fixture_pae(PAYLOAD_TYPE, self.capability_payload), KEY_ID, SIGNATURE)],
        )

    def test_valid_receipt_returns_the_exact_authenticated_payload(self):
        verified, callback = self.verify_receipt()

        self.assertEqual(verified.signer_id, SIGNER_ID)
        self.assertEqual(verified.payload, self.receipt_payload)
        self.assertEqual(
            verified.payload_sha256,
            hashlib.sha256(self.receipt_payload).hexdigest(),
        )
        self.assertEqual(json.loads(verified.payload), self.receipt_statement)
        self.assertEqual(
            callback.calls,
            [
                (fixture_pae(PAYLOAD_TYPE, self.capability_payload), KEY_ID, SIGNATURE),
                (fixture_pae(PAYLOAD_TYPE, self.receipt_payload), KEY_ID, SIGNATURE),
            ],
        )

    def test_keyid_is_only_a_lookup_hint(self):
        envelope = json.loads(self.capability_envelope)
        envelope["signatures"][0]["keyid"] = "untrusted-alias"
        envelope_bytes = fixture_json_bytes(envelope)
        verified, callback = self.verify_capability(envelope=envelope_bytes)

        self.assertEqual(verified.signer_id, SIGNER_ID)
        self.assertEqual(callback.calls[0][1], "untrusted-alias")

    def test_underwrite_profile_requires_a_nonempty_keyid_hint(self):
        envelope = fixture_envelope(self.capability_payload, omit_keyid=True)
        callback = self.assert_capability_rejected(envelope=envelope)

        self.assertEqual(callback.calls, [])


class StrictEnvelopeParsing(ReceiptCase):
    def test_rejects_invalid_envelope_json_before_signature_verification(self):
        text = self.capability_envelope.decode("utf-8")
        bad_envelopes = {
            "invalid utf8": b"\xff",
            "bom": b"\xef\xbb\xbf" + self.capability_envelope,
            "trailing document": self.capability_envelope + b"{}",
            "nonfinite": (text[:-1] + ',"unknown":NaN}').encode("utf-8"),
            "surrogate": (text[:-1] + ',"unknown":"\\ud800"}').encode("utf-8"),
            "nonobject": b"[]",
        }
        for name, envelope in bad_envelopes.items():
            with self.subTest(name=name):
                callback = self.assert_capability_rejected(envelope=envelope)
                self.assertEqual(callback.calls, [])

    def test_rejects_duplicate_envelope_keys_recursively(self):
        text = self.capability_envelope.decode("utf-8")
        encoded_payload = base64.b64encode(self.capability_payload).decode("ascii")
        duplicates = {
            "payload": text.replace(
                '"payload":', '"payload":"' + encoded_payload + '","payload":', 1
            ),
            "signature keyid": text.replace(
                '"keyid":', '"keyid":"attacker-key","keyid":', 1
            ),
            "unknown": text.replace("{", '{"unknown":1,"unknown":2,', 1),
        }
        for name, duplicate in duplicates.items():
            with self.subTest(name=name):
                callback = self.assert_capability_rejected(
                    envelope=duplicate.encode("utf-8")
                )
                self.assertEqual(callback.calls, [])

    def test_rejects_invalid_signed_json_only_after_signature_verification(self):
        text = self.capability_payload.decode("utf-8")
        bad_payloads = {
            "invalid utf8": b"\xff",
            "bom": b"\xef\xbb\xbf" + self.capability_payload,
            "trailing document": self.capability_payload + b"{}",
            "nonfinite": (
                text.replace('"issuedAt":', '"unknown":NaN,"issuedAt":', 1)
            ).encode("utf-8"),
            "surrogate": text.replace(CHALLENGE, "\\ud800", 1).encode("utf-8"),
            "duplicate predicate": text.replace(
                '"executor":',
                '"executor":{"id":"attacker"},"executor":',
                1,
            ).encode("utf-8"),
            "duplicate nested target": text.replace(
                '"repo":', '"repo":"attacker/widget","repo":', 1
            ).encode("utf-8"),
        }
        for name, payload in bad_payloads.items():
            with self.subTest(name=name):
                envelope = fixture_envelope(payload)
                callback = self.assert_capability_rejected(envelope=envelope)
                self.assertEqual(len(callback.calls), 1)
                self.assertEqual(
                    callback.calls[0][0], fixture_pae(PAYLOAD_TYPE, payload)
                )

    def test_rejects_noncanonical_base64_before_signature_verification(self):
        parsed = json.loads(self.capability_envelope)
        mutations = {}
        for name, signature in {
            "invalid character": "+/8%",
            "whitespace": "+/8=\n",
            "missing padding": "+/8",
            "extra padding": "+/8==",
            "nonzero pad bits": "+/9=",
            "mixed alphabet one": "+_8=",
            "mixed alphabet two": "-/8=",
        }.items():
            envelope = copy.deepcopy(parsed)
            envelope["signatures"][0]["sig"] = signature
            mutations[name] = fixture_json_bytes(envelope)

        for name, envelope in mutations.items():
            with self.subTest(name=name):
                callback = self.assert_capability_rejected(envelope=envelope)
                self.assertEqual(callback.calls, [])

    def test_accepts_canonical_url_safe_base64(self):
        parsed = json.loads(self.capability_envelope)
        parsed["signatures"][0]["sig"] = "-_8="

        verified, callback = self.verify_capability(
            envelope=fixture_json_bytes(parsed)
        )

        self.assertEqual(verified.signer_id, SIGNER_ID)
        self.assertEqual(callback.calls[0][2], SIGNATURE)

    def test_requires_exactly_one_signature(self):
        parsed = json.loads(self.capability_envelope)
        for name, signatures in {
            "none": [],
            "two": parsed["signatures"] * 2,
        }.items():
            with self.subTest(name=name):
                envelope = copy.deepcopy(parsed)
                envelope["signatures"] = signatures
                callback = self.assert_capability_rejected(
                    envelope=fixture_json_bytes(envelope)
                )
                self.assertEqual(callback.calls, [])

    def test_requires_exact_payload_type_and_signature_member_types(self):
        parsed = json.loads(self.capability_envelope)
        cases = {}
        wrong_type = copy.deepcopy(parsed)
        wrong_type["payloadType"] = PAYLOAD_TYPE.upper()
        cases["payload type"] = wrong_type
        nonstring_keyid = copy.deepcopy(parsed)
        nonstring_keyid["signatures"][0]["keyid"] = 7
        cases["keyid type"] = nonstring_keyid
        missing_sig = copy.deepcopy(parsed)
        del missing_sig["signatures"][0]["sig"]
        cases["missing sig"] = missing_sig

        for name, value in cases.items():
            with self.subTest(name=name):
                callback = self.assert_capability_rejected(
                    envelope=fixture_json_bytes(value)
                )
                self.assertEqual(callback.calls, [])

    def test_ignores_dsse_extensions_without_accepting_algorithm_control(self):
        parsed = json.loads(self.capability_envelope)
        parsed["transportExtension"] = {"critical": False}
        parsed["signatures"][0]["alg"] = "none"

        verified, callback = self.verify_capability(
            envelope=fixture_json_bytes(parsed)
        )

        self.assertEqual(verified.signer_id, SIGNER_ID)
        self.assertEqual(len(callback.calls), 1)


class SignatureBoundary(ReceiptCase):
    def test_signature_callback_failure_is_a_receipt_error(self):
        callback = SignatureRecorder(error_at=0)

        returned = self.assert_capability_rejected(callback=callback)

        self.assertIs(returned, callback)
        self.assertEqual(len(callback.calls), 1)

    def test_signature_callback_must_return_an_authenticated_identity(self):
        for returned in (None, False, True, "", 7):
            with self.subTest(returned=returned):
                callback = SignatureRecorder(returned)
                self.assert_capability_rejected(callback=callback)
                self.assertEqual(len(callback.calls), 1)

    def test_authenticated_signer_and_signed_executor_are_independently_bound(self):
        wrong_signer = SignatureRecorder(OTHER_SIGNER_ID)
        self.assert_capability_rejected(callback=wrong_signer)

        statement = copy.deepcopy(self.capability_statement)
        statement["predicate"]["executor"]["id"] = OTHER_EXECUTOR_ID
        envelope, _payload = self.encode_capability(statement)
        callback = self.assert_capability_rejected(envelope=envelope)
        self.assertEqual(len(callback.calls), 1)

        expected = self.capability_expected()
        expected["executorId"] = OTHER_EXECUTOR_ID
        self.assert_capability_rejected(expected=expected)

    def test_execution_signer_must_match_capability_signer(self):
        callback = SignatureRecorder(SIGNER_ID, OTHER_SIGNER_ID)

        self.assert_receipt_rejected(callback=callback)

        self.assertEqual(len(callback.calls), 2)


class StrictSignedSchema(ReceiptCase):
    def test_rejects_wrong_statement_identity_subject_and_predicate_type(self):
        mutations = []
        wrong_statement = copy.deepcopy(self.capability_statement)
        wrong_statement["_type"] = "https://in-toto.io/Statement/v0.1"
        mutations.append(("statement type", wrong_statement))
        wrong_predicate = copy.deepcopy(self.capability_statement)
        wrong_predicate["predicateType"] = RECEIPT_TYPE
        mutations.append(("predicate type", wrong_predicate))
        wrong_subject_name = copy.deepcopy(self.capability_statement)
        wrong_subject_name["subject"][0]["name"] = "underwrite-execution-output"
        mutations.append(("subject name", wrong_subject_name))
        wrong_subject_tree = copy.deepcopy(self.capability_statement)
        wrong_subject_tree["subject"][0]["digest"]["gitTree"] = OUTPUT_TREE
        mutations.append(("subject tree", wrong_subject_tree))
        extra_subject = copy.deepcopy(self.capability_statement)
        extra_subject["subject"].append(copy.deepcopy(extra_subject["subject"][0]))
        mutations.append(("extra subject", extra_subject))

        for name, statement in mutations:
            with self.subTest(name=name):
                envelope, _payload = self.encode_capability(statement)
                self.assert_capability_rejected(envelope=envelope)

    def test_rejects_unknown_signed_fields_at_every_security_layer(self):
        mutations = []
        for name, path in (
            ("statement", ()),
            ("subject", ("subject", 0)),
            ("predicate", ("predicate",)),
            ("executor", ("predicate", "executor")),
            ("invocation", ("predicate", "invocation")),
            ("session", ("predicate", "invocation", "session")),
            ("target", ("predicate", "invocation", "target")),
            ("sandbox", ("predicate", "invocation", "sandbox")),
            ("limits", ("predicate", "invocation", "sandbox", "limits")),
        ):
            statement = copy.deepcopy(self.capability_statement)
            current = statement
            for component in path:
                current = current[component]
            current["unknown"] = "ignored security claim"
            mutations.append((name, statement))

        for name, statement in mutations:
            with self.subTest(name=name):
                envelope, _payload = self.encode_capability(statement)
                self.assert_capability_rejected(envelope=envelope)

    def test_rejects_missing_required_signed_fields(self):
        paths = (
            ("_type",),
            ("subject",),
            ("predicate", "executor"),
            ("predicate", "issuedAt"),
            ("predicate", "expiresAt"),
            ("predicate", "invocation", "session", "challenge"),
            ("predicate", "invocation", "target", "digest"),
            ("predicate", "invocation", "sourceBundle", "bytes"),
            ("predicate", "invocation", "jobDigest"),
            ("predicate", "invocation", "inputTree"),
            ("predicate", "invocation", "sandbox", "limits"),
        )
        for path in paths:
            with self.subTest(path=path):
                statement = copy.deepcopy(self.capability_statement)
                current = statement
                for component in path[:-1]:
                    current = current[component]
                del current[path[-1]]
                envelope, _payload = self.encode_capability(statement)
                self.assert_capability_rejected(envelope=envelope)


class CapabilityBindings(ReceiptCase):
    def test_expected_target_time_and_executable_identity_are_strict(self):
        merged_at = self.capability_expected()
        merged_at["target"]["merged_at"] = "yesterday"
        self.assert_capability_rejected(expected=merged_at)

        executable = self.capability_expected()
        executable["job"]["argv"][0] = "/usr/bin/env"
        self.assert_capability_rejected(expected=executable)

    def test_rejects_every_frozen_target_field_mutation(self):
        for field, value in self.target.items():
            with self.subTest(field=field):
                expected = self.capability_expected()
                if value is None:
                    replacement = "2026-08-29T00:00:00Z"
                elif type(value) is int:
                    replacement = value + 1
                else:
                    replacement = value[:-1] + ("0" if value[-1:] != "0" else "1")
                expected["target"][field] = replacement
                self.assert_capability_rejected(expected=expected)

    def test_rejects_session_challenge_action_job_and_input_tree_mutations(self):
        changes = []
        changed_session = self.capability_expected()
        changed_session["sessionId"] = "00000000-0000-4000-8000-000000000001"
        changes.append(("session", changed_session))
        changed_challenge = self.capability_expected()
        changed_challenge["challenge"] = "8" * 64
        changes.append(("challenge", changed_challenge))
        for field in self.action:
            changed_action = self.capability_expected()
            changed_action["action"][field] += 1
            changes.append(("action " + field, changed_action))
        changed_job = self.capability_expected()
        changed_job["job"]["argv"] = [
            self.job["argv"][0],
            self.job["argv"][2],
            self.job["argv"][1],
        ]
        changes.append(("ordered argv", changed_job))
        repartitioned_job = self.capability_expected()
        repartitioned_job["job"]["argv"] = [
            "/usr/bin/python3",
            "-cprint('café')",
        ]
        changes.append(("argv boundaries", repartitioned_job))
        changed_tree = self.capability_expected()
        changed_tree["inputTree"] = "9" * 64
        changes.append(("input tree", changed_tree))

        for name, expected in changes:
            with self.subTest(name=name):
                self.assert_capability_rejected(expected=expected)

    def test_rejects_signed_target_job_and_source_bundle_digest_mismatches(self):
        mutations = []
        wrong_target_digest = copy.deepcopy(self.capability_statement)
        wrong_target_digest["predicate"]["invocation"]["target"]["digest"][
            "sha256"
        ] = "0" * 64
        mutations.append(("target digest", wrong_target_digest))
        wrong_job_digest = copy.deepcopy(self.capability_statement)
        wrong_job_digest["predicate"]["invocation"]["jobDigest"]["sha256"] = (
            "0" * 64
        )
        mutations.append(("job digest", wrong_job_digest))
        wrong_bundle_digest = copy.deepcopy(self.capability_statement)
        wrong_bundle_digest["predicate"]["invocation"]["sourceBundle"]["sha256"] = (
            "0" * 64
        )
        mutations.append(("source bundle digest", wrong_bundle_digest))
        wrong_bundle_size = copy.deepcopy(self.capability_statement)
        wrong_bundle_size["predicate"]["invocation"]["sourceBundle"]["bytes"] += 1
        mutations.append(("source bundle bytes", wrong_bundle_size))

        for name, statement in mutations:
            with self.subTest(name=name):
                envelope, _payload = self.encode_capability(statement)
                self.assert_capability_rejected(envelope=envelope)

    def test_rejects_policy_downgrades_and_implicit_defaults(self):
        def mutate(field, value):
            statement = copy.deepcopy(self.capability_statement)
            statement["predicate"]["invocation"]["sandbox"][field] = value
            return statement

        mutations = {
            "policy": mutate("policy", SANDBOX_POLICY_TYPE + "/unknown"),
            "credentials": mutate("credentials", "inherited"),
            "network": mutate("network", "allowed"),
            "host writes": mutate("hostWrites", "workspace"),
            "git hooks": mutate("gitHooks", "enabled"),
            "git filters": mutate("gitFilters", "enabled"),
            "timeout": mutate("timeout", "best-effort"),
        }
        for limit in self.sandbox["limits"]:
            statement = copy.deepcopy(self.capability_statement)
            statement["predicate"]["invocation"]["sandbox"]["limits"][limit] += 1
            mutations["limit " + limit] = statement
        missing = copy.deepcopy(self.capability_statement)
        del missing["predicate"]["invocation"]["sandbox"]["network"]
        mutations["missing network"] = missing
        bool_limit = copy.deepcopy(self.capability_statement)
        bool_limit["predicate"]["invocation"]["sandbox"]["limits"][
            "processes"
        ] = True
        mutations["boolean limit"] = bool_limit

        for name, statement in mutations.items():
            with self.subTest(name=name):
                envelope, _payload = self.encode_capability(statement)
                self.assert_capability_rejected(envelope=envelope)


class TimeValidity(ReceiptCase):
    def test_capability_time_window_is_closed_at_expiration(self):
        verified, _callback = self.verify_capability(
            now=datetime(2026, 8, 29, 11, 55, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(verified.signer_id, SIGNER_ID)

        self.assert_capability_rejected(
            now=datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
        )

    def test_rejects_invalid_capability_times_and_naive_now(self):
        cases = {
            "issued in future": (
                "2026-08-29T12:01:00Z",
                "2026-08-29T12:04:00Z",
            ),
            "expires before issue": (ISSUED_AT, "2026-08-29T11:54:59Z"),
            "noncanonical offset": (
                "2026-08-29T06:55:00-05:00",
                EXPIRES_AT,
            ),
            "overprecise fraction": (
                "2026-08-29T11:55:00.0000000Z",
                EXPIRES_AT,
            ),
            "malformed": ("not-a-time", EXPIRES_AT),
        }
        for name, (issued, expires) in cases.items():
            with self.subTest(name=name):
                statement = copy.deepcopy(self.capability_statement)
                statement["predicate"]["issuedAt"] = issued
                statement["predicate"]["expiresAt"] = expires
                envelope, _payload = self.encode_capability(statement)
                self.assert_capability_rejected(envelope=envelope)

        self.assert_capability_rejected(now=NOW.replace(tzinfo=None))

    def test_execution_times_must_be_ordered_current_and_within_capability(self):
        cases = {
            "starts before capability": ("2026-08-29T11:54:59Z", FINISHED_AT),
            "finishes before start": (STARTED_AT, "2026-08-29T11:58:59Z"),
            "finishes in future": (STARTED_AT, "2026-08-29T12:00:01Z"),
            "exceeds wall limit": (
                "2026-08-29T11:58:00Z",
                "2026-08-29T11:59:30Z",
            ),
            "noncanonical start": ("2026-08-29T06:59:00-05:00", FINISHED_AT),
        }
        for name, (started, finished) in cases.items():
            with self.subTest(name=name):
                statement = copy.deepcopy(self.receipt_statement)
                statement["predicate"]["startedAt"] = started
                statement["predicate"]["finishedAt"] = finished
                envelope, _payload = self.encode_receipt(statement)
                self.assert_receipt_rejected(receipt_envelope=envelope)


class ExecutionBindings(ReceiptCase):
    def test_capability_link_is_the_digest_of_exact_signed_payload_bytes(self):
        pretty_envelope, pretty_payload = self.encode_capability(pretty=True)
        self.assertNotEqual(pretty_payload, self.capability_payload)

        callback = self.assert_receipt_rejected(
            capability_envelope=pretty_envelope
        )
        self.assertEqual(len(callback.calls), 2)

        statement = self.make_receipt_statement(pretty_payload)
        receipt_envelope, receipt_payload = self.encode_receipt(statement)
        verified, callback = self.verify_receipt(
            capability_envelope=pretty_envelope,
            receipt_envelope=receipt_envelope,
        )
        self.assertEqual(verified.payload, receipt_payload)
        self.assertEqual(len(callback.calls), 2)

    def test_rejects_receipt_context_and_capability_digest_mutations(self):
        mutations = []
        wrong_capability = copy.deepcopy(self.receipt_statement)
        wrong_capability["predicate"]["capability"]["payloadSha256"] = "0" * 64
        mutations.append(("capability digest", wrong_capability))
        for path, label in (
            (("session", "id"), "session"),
            (("session", "challenge"), "challenge"),
            (("action", "seq"), "action seq"),
            (("action", "beat"), "action beat"),
            (("action", "attempt"), "action attempt"),
            (("inputTree", "gitTree"), "input tree"),
        ):
            statement = copy.deepcopy(self.receipt_statement)
            value = statement["predicate"]["invocation"]
            for component in path[:-1]:
                value = value[component]
            original = value[path[-1]]
            value[path[-1]] = (
                original + 1 if type(original) is int else "9" * len(original)
            )
            mutations.append((label, statement))

        for name, statement in mutations:
            with self.subTest(name=name):
                envelope, _payload = self.encode_receipt(statement)
                self.assert_receipt_rejected(receipt_envelope=envelope)

    def test_rejects_output_tree_and_bundle_cross_representation_mismatches(self):
        mutations = []
        subject = copy.deepcopy(self.receipt_statement)
        subject["subject"][0]["digest"]["gitTree"] = "9" * 64
        mutations.append(("subject tree", subject))
        output_tree = copy.deepcopy(self.receipt_statement)
        output_tree["predicate"]["outputTree"]["gitTree"] = "9" * 64
        mutations.append(("predicate tree", output_tree))
        bundle_tree = copy.deepcopy(self.receipt_statement)
        bundle_tree["predicate"]["outputBundle"]["gitTree"] = "9" * 64
        mutations.append(("bundle tree", bundle_tree))
        bundle_digest = copy.deepcopy(self.receipt_statement)
        bundle_digest["predicate"]["outputBundle"]["sha256"] = "9" * 64
        mutations.append(("bundle digest", bundle_digest))
        bundle_bytes = copy.deepcopy(self.receipt_statement)
        bundle_bytes["predicate"]["outputBundle"]["bytes"] += 1
        mutations.append(("bundle bytes", bundle_bytes))

        for name, statement in mutations:
            with self.subTest(name=name):
                envelope, _payload = self.encode_receipt(statement)
                self.assert_receipt_rejected(receipt_envelope=envelope)

    def test_rejects_stream_descriptor_mismatches_and_truncation(self):
        mutations = []
        for stream in ("stdout", "stderr"):
            for field in ("sha256", "bytes", "truncated"):
                statement = copy.deepcopy(self.receipt_statement)
                value = statement["predicate"]["streams"][stream][field]
                if field == "sha256":
                    replacement = "9" * 64
                elif field == "bytes":
                    replacement = value + 1
                else:
                    replacement = True
                statement["predicate"]["streams"][stream][field] = replacement
                mutations.append((stream + " " + field, statement))

        for name, statement in mutations:
            with self.subTest(name=name):
                envelope, _payload = self.encode_receipt(statement)
                self.assert_receipt_rejected(receipt_envelope=envelope)

    def test_rejects_matching_streams_that_exceed_the_total_output_limit(self):
        expected = copy.deepcopy(self.expected)
        limit = expected["sandbox"]["limits"]["outputBytes"]
        expected["stdout"]["bytes"] = limit
        expected["stderr"]["bytes"] = 1
        statement = copy.deepcopy(self.receipt_statement)
        statement["predicate"]["streams"] = {
            "stdout": copy.deepcopy(expected["stdout"]),
            "stderr": copy.deepcopy(expected["stderr"]),
        }
        envelope, _payload = self.encode_receipt(statement)

        self.assert_receipt_rejected(
            receipt_envelope=envelope,
            expected=expected,
        )

    def test_rejects_any_incomplete_success_result(self):
        mutations = {
            "status": ("status", "signaled"),
            "exit code": ("exitCode", 1),
            "signal": ("signal", "SIGKILL"),
            "timeout": ("timedOut", True),
            "resource violation": ("resourceViolation", "memory"),
            "isolation violation": ("isolationViolation", "network"),
            "surviving processes": ("survivingProcesses", 1),
            "teardown": ("teardown", "incomplete"),
        }
        for name, (field, value) in mutations.items():
            with self.subTest(name=name):
                statement = copy.deepcopy(self.receipt_statement)
                statement["predicate"]["result"][field] = value
                envelope, _payload = self.encode_receipt(statement)
                self.assert_receipt_rejected(receipt_envelope=envelope)

    def test_rejects_wrong_expected_delivery_values(self):
        mutations = []
        output_tree = copy.deepcopy(self.expected)
        output_tree["outputTree"] = "9" * 64
        mutations.append(("output tree", output_tree))
        output_digest = copy.deepcopy(self.expected)
        output_digest["outputBundle"]["sha256"] = "9" * 64
        mutations.append(("bundle digest", output_digest))
        output_bytes = copy.deepcopy(self.expected)
        output_bytes["outputBundle"]["bytes"] += 1
        mutations.append(("bundle bytes", output_bytes))
        stdout = copy.deepcopy(self.expected)
        stdout["stdout"]["bytes"] += 1
        mutations.append(("stdout", stdout))
        stderr = copy.deepcopy(self.expected)
        stderr["stderr"]["sha256"] = "9" * 64
        mutations.append(("stderr", stderr))
        exit_code = copy.deepcopy(self.expected)
        exit_code["exitCode"] = 1
        mutations.append(("exit code", exit_code))

        for name, expected in mutations:
            with self.subTest(name=name):
                self.assert_receipt_rejected(expected=expected)

    def test_stateless_revalidation_is_idempotent_but_cross_context_replay_fails(self):
        first, _callback = self.verify_receipt()
        second, _callback = self.verify_receipt()
        self.assertEqual(first.payload_sha256, second.payload_sha256)

        expected = copy.deepcopy(self.expected)
        expected["challenge"] = "8" * 64
        self.assert_receipt_rejected(expected=expected)


class ArchitecturalIsolation(unittest.TestCase):
    def test_receipt_module_import_does_not_load_session_store(self):
        program = "\n".join(
            (
                "import sys",
                "sys.path.insert(0, sys.argv[1])",
                "import execution_receipt",
                "assert 'session_store' not in sys.modules",
            )
        )
        done = subprocess.run(
            [sys.executable, "-I", "-c", program, str(SCRIPTS)],
            capture_output=True,
            text=True,
        )

        self.assertEqual(done.returncode, 0, done.stderr)

    def test_receipt_protocol_has_no_store_or_execution_policy_integration(self):
        receipt_source = (SCRIPTS / "execution_receipt.py").read_text(encoding="utf-8")
        tree = ast.parse(receipt_source)
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
        self.assertNotIn("session_store", imports)
        self.assertNotIn("SessionStore", receipt_source)
        self.assertNotIn("check_execution", receipt_source)

        store_source = (SCRIPTS / "session_store.py").read_text(encoding="utf-8")
        cli_source = (SCRIPTS / "sessionctl.py").read_text(encoding="utf-8")
        self.assertNotIn("execution_receipt", store_source)
        self.assertNotIn("execution_receipt", cli_source)
        self.assertIn('EXECUTION_MODES = ("no_exec",)', store_source)


if __name__ == "__main__":
    unittest.main()
