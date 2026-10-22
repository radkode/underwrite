#!/usr/bin/env python3
"""Run one walk claim on the frozen PR head through the execution gateway.

A PR session never executes the head. A proof does not change that: the gateway runs the
command on its own quarantine of the frozen head, inside its sandbox, and signs a receipt
only when the command exits with the code the walk predicted. This module reserves that
request, consumes the gateway's evidence, verifies every signature and digest against the
trusted profile and pinned key, and records the verified run so one beat can cite it.
"""

import copy
import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from attested_implementation import (
    ImplementationError,
    _JSON_BYTES,
    _bounded_json_integer,
    _canonical_bytes,
    _evidence_directory,
    _persist_files,
    _profile,
    _read_file,
    _read_json,
    _stream_descriptor,
    _unique_object,
    _verify_persisted_files,
)
from execution_receipt import verify_execution_receipt
from gateway import artifacts
from gateway.signing import OpenSSLVerifier
from session_store import Conflict, SessionStore, StoreError


PROOF_EVIDENCE_DIRECTORY = "proof-evidence"
# The output bundle is verified against the signed output tree and then let go: a proof
# is what the command printed and how it exited, never the tree it left behind.
_PERSISTED = (
    "request.json",
    "capability.dsse.json",
    "receipt.dsse.json",
    "stdout",
    "stderr",
)
_ADAPTER_SECONDS = 1200
_ADAPTER_OUTPUT_BYTES = 64 * 1024
_CONFIG_FIELDS = {"version", "profile", "publicKey", "adapter", "evidenceRoot"}


class ProofError(ImplementationError):
    """The proof cannot be reserved, run, or verified safely."""


def _public(attempt):
    result = copy.deepcopy(attempt)
    result.pop("capability", None)
    result.pop("receipt", None)
    return result


def _proof_profile(profile, request):
    """The request is the profile's own job with arguments after its command."""
    job = request["job"]
    prefix = profile["job"]["argv"]
    if job["argv"][: len(prefix)] != prefix or len(job["argv"]) <= len(prefix):
        raise Conflict("proof command does not extend the trusted profile command")
    for name in ("cwd", "environment", "executable", "stdin"):
        if job[name] != profile["job"][name]:
            raise Conflict(f"proof job {name} does not match the trusted profile")
    if request["sandbox"] != profile["sandbox"]:
        raise Conflict("proof sandbox does not match the trusted profile")


def request(session_root, beat, profile_source, arguments, exit_code):
    """Reserve one proof for a beat and return the exact gateway request."""
    store = SessionStore(session_root)
    store.verify_target_files()
    attempt = store.reserve_proof(beat, _profile(profile_source), arguments, exit_code)
    return _public(attempt)


def fail(session_root, proof, reason):
    """Record that the gateway proved this attempt can never produce a receipt."""
    return _public(SessionStore(session_root).fail_proof(proof, reason))


def proofs(session_root):
    return {"proofs": [_public(item) for item in SessionStore(session_root).proof_attempts()]}


def _verified_replay(store, attempt, values, output_bundle):
    evidence = attempt["evidence"]
    checks = {
        "requestSha256": hashlib.sha256(values["request.json"]).hexdigest(),
        "capabilitySha256": hashlib.sha256(values["capability.dsse.json"]).hexdigest(),
        "receiptSha256": hashlib.sha256(values["receipt.dsse.json"]).hexdigest(),
        "outputBundle": {
            "sha256": hashlib.sha256(output_bundle).hexdigest(),
            "bytes": len(output_bundle),
        },
        "stdout": _stream_descriptor(values["stdout"]),
        "stderr": _stream_descriptor(values["stderr"]),
    }
    for name, value in checks.items():
        if evidence.get(name) != value:
            raise Conflict(f"verified proof replay changed {name}")
    _verify_persisted_files(
        store.root / PROOF_EVIDENCE_DIRECTORY / str(attempt["proof"]),
        values,
        "proof evidence",
    )
    return attempt


def consume(session_root, proof, profile_source, public_key, evidence_dir, *, now=None):
    """Verify the gateway's six-file handoff for one proof and record it."""
    store = SessionStore(session_root)
    profile = _profile(profile_source)
    directory = _evidence_directory(evidence_dir)
    request_bytes = _read_file(directory / "request.json", "execution request", _JSON_BYTES)
    try:
        request_value = json.loads(
            request_bytes.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_int=_bounded_json_integer,
        )
    except ImplementationError:
        raise
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ProofError("execution request is not valid UTF-8 JSON") from error
    try:
        coordinate = request_value["action"]["seq"]
    except (KeyError, TypeError) as error:
        raise ProofError("execution request has no proof coordinate") from error
    if coordinate != proof:
        raise Conflict(f"evidence is for proof {coordinate}, not proof {proof}")
    attempt = store.proof_attempt(proof)
    if request_bytes != _canonical_bytes(attempt["request"]):
        raise Conflict("evidence request bytes do not match the reserved proof")
    if attempt["trusted_profile"] != profile:
        raise Conflict("trusted execution profile changed after proof reservation")
    reserved = attempt["request"]
    _proof_profile(profile, reserved)
    limits = profile["sandbox"]["limits"]
    workspace_limit = limits["workspaceBytes"]
    output_limit = limits["outputBytes"]
    bundle_limit = workspace_limit * 2
    capability = _read_file(directory / "capability.dsse.json", "host capability", _JSON_BYTES)
    receipt = _read_file(directory / "receipt.dsse.json", "execution receipt", _JSON_BYTES)
    output_bundle = _read_file(directory / "output.bundle", "output bundle", bundle_limit)
    stdout = _read_file(directory / "stdout", "execution stdout", output_limit)
    stderr = _read_file(directory / "stderr", "execution stderr", output_limit)
    if len(stdout) + len(stderr) > output_limit:
        raise ProofError("execution streams exceed the trusted output limit")
    verifier = OpenSSLVerifier(public_key, profile["signerId"])
    if verifier.key_id != profile["keyId"]:
        raise Conflict("public key does not match the trusted profile key ID")
    values = {
        "request.json": request_bytes,
        "capability.dsse.json": capability,
        "receipt.dsse.json": receipt,
        "stdout": stdout,
        "stderr": stderr,
    }
    if attempt["state"] == "verified":
        return _public(_verified_replay(store, attempt, values, output_bundle))
    if attempt["state"] != "reserved":
        raise Conflict(f"proof {proof} is {attempt['state']}, not reserved")
    target = store.verify_target_files()
    if target != reserved["target"]:
        raise Conflict("proof request target is not the frozen session target")
    with tempfile.TemporaryDirectory(prefix="underwrite-proof-") as temporary:
        input_result = artifacts.verify_source_bundle(
            store.read_object_bundle(),
            target,
            Path(temporary) / "input",
            maximum_workspace_bytes=workspace_limit,
        )
    output = artifacts.verify_output_bundle(
        output_bundle,
        maximum_workspace_bytes=workspace_limit,
        maximum_bundle_bytes=bundle_limit,
    )
    expected = {
        "signerId": profile["signerId"],
        "executorId": profile["executorId"],
        "sessionId": reserved["sessionId"],
        "challenge": attempt["challenge"],
        "target": target,
        "action": copy.deepcopy(reserved["action"]),
        "job": copy.deepcopy(reserved["job"]),
        "inputTree": input_result.git_tree,
        "sandbox": copy.deepcopy(profile["sandbox"]),
        "outputTree": output.git_tree,
        "outputBundle": {"sha256": output.sha256, "bytes": output.bytes},
        "stdout": _stream_descriptor(stdout),
        "stderr": _stream_descriptor(stderr),
        "exitCode": reserved["exitCode"],
    }
    verified = verify_execution_receipt(
        capability,
        receipt,
        expected,
        verifier.verify,
        now or datetime.now(timezone.utc),
    )
    evidence = {
        "version": 1,
        "requestSha256": attempt["request_sha256"],
        "keyId": profile["keyId"],
        "signerId": verified.signer_id,
        "executorId": profile["executorId"],
        "capabilitySha256": hashlib.sha256(capability).hexdigest(),
        "receiptSha256": hashlib.sha256(receipt).hexdigest(),
        "inputTree": input_result.git_tree,
        "outputTree": output.git_tree,
        "outputBundle": {"sha256": output.sha256, "bytes": output.bytes},
        "stdout": _stream_descriptor(stdout),
        "stderr": _stream_descriptor(stderr),
        "exitCode": reserved["exitCode"],
    }
    _persist_files(store.root, PROOF_EVIDENCE_DIRECTORY, str(proof), values, "proof evidence")
    return _public(
        store.record_verified_proof(proof, capability, receipt, evidence, stdout, stderr)
    )


def _absolute(value, label):
    if not isinstance(value, str) or not os.path.isabs(value):
        raise ProofError(f"proof configuration {label} must be an absolute path")
    return value


def load_configuration(path):
    """The controller's own record of where the host's proof gateway lives.

    Host-provisioned and outside every repository and session, like the profile and key
    it names. Nothing in it comes from the head or from returned evidence."""
    value = _read_json(path, "proof configuration")
    if set(value) != _CONFIG_FIELDS or value.get("version") != 1:
        raise ProofError(
            "proof configuration must be version 1 with exactly "
            + ", ".join(sorted(_CONFIG_FIELDS))
        )
    for name in ("profile", "publicKey", "evidenceRoot"):
        _absolute(value[name], name)
    adapter = value["adapter"]
    if (
        not isinstance(adapter, list)
        or not adapter
        or any(not isinstance(part, str) or not part or "\x00" in part for part in adapter)
    ):
        raise ProofError("proof configuration adapter must be a non-empty argv list")
    _absolute(adapter[0], "adapter[0]")
    return value


def _adapter_outcome(completed):
    try:
        result = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeError, ValueError):
        result = None
    detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
    detail = detail[-1] if detail else ""
    if not isinstance(result, dict):
        return "retry", detail or "adapter printed no result"
    status = result.get("status")
    if status == "complete" and completed.returncode == 0:
        return "complete", result.get("evidenceDir")
    if status == "failed" and completed.returncode == 2:
        return "failed", detail or result.get("reason") or "gateway attempt is terminal"
    return "retry", detail or result.get("reason") or "unknown adapter outcome"


def prove(session_root, beat, configuration_path, arguments, exit_code, *, now=None):
    """Reserve, run through the host adapter, and consume one proof in a single call.

    `failed` means the gateway proved it will never sign this attempt, most often
    because the command did not exit as predicted. `retry` leaves the same reservation
    for a byte-identical rerun."""
    configuration = load_configuration(configuration_path)
    store = SessionStore(session_root)
    attempt = request(session_root, beat, configuration["profile"], arguments, exit_code)
    proof = attempt["proof"]
    if attempt["state"] == "verified":
        return {"status": "verified", "proof": attempt}
    if attempt["state"] != "reserved":
        raise Conflict(f"proof {proof} is {attempt['state']}")
    session_id = store.delivery_state()["session_id"]
    evidence_dir = os.path.join(
        configuration["evidenceRoot"], f"underwrite-{session_id}-proof-{proof}"
    )
    with tempfile.TemporaryDirectory(prefix="underwrite-proof-request-") as temporary:
        request_path = Path(temporary) / "request.json"
        request_path.write_bytes(_canonical_bytes(attempt["request"]))
        request_path.chmod(0o644)
        Path(temporary).chmod(0o755)
        try:
            completed = subprocess.run(
                [
                    *configuration["adapter"],
                    "--request", str(request_path),
                    "--source-bundle", str(store.root / "pr.bundle"),
                    "--evidence-dir", evidence_dir,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=_ADAPTER_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "retry",
                "proof": attempt,
                "reason": "the gateway adapter did not finish in time",
            }
        except OSError as error:
            raise ProofError(f"gateway adapter could not be started: {error}") from error
    completed.stdout = completed.stdout[:_ADAPTER_OUTPUT_BYTES]
    completed.stderr = completed.stderr[-_ADAPTER_OUTPUT_BYTES:]
    status, detail = _adapter_outcome(completed)
    if status == "failed":
        return {"status": "failed", "proof": fail(session_root, proof, detail)}
    if status == "retry":
        return {"status": "retry", "proof": attempt, "reason": detail}
    if detail != evidence_dir:
        raise Conflict("gateway adapter published evidence somewhere other than asked")
    verified = consume(
        session_root,
        proof,
        configuration["profile"],
        configuration["publicKey"],
        evidence_dir,
        now=now,
    )
    return {"status": "verified", "proof": verified}
