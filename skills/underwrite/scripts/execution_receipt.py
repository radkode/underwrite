"""Fail-closed validation for Underwrite host execution attestations."""

import base64
import binascii
import copy
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone


DSSE_PAYLOAD_TYPE = "application/vnd.in-toto+json"
STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
CAPABILITY_PREDICATE_TYPE = (
    "https://github.com/radkode/underwrite/attestations/host-capability/v1"
)
EXECUTION_PREDICATE_TYPE = (
    "https://github.com/radkode/underwrite/attestations/execution-receipt/v1"
)
SANDBOX_POLICY_TYPE = (
    "https://github.com/radkode/underwrite/sandbox-policy/v1"
)
MAX_ENVELOPE_BYTES = 1_000_000
MAX_PAYLOAD_BYTES = 512_000
MAX_SIGNATURE_BYTES = 16_384
MAX_CAPABILITY_SECONDS = 300
MAX_JSON_INTEGER = 9_007_199_254_740_991

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OBJECT = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_UTC_TIMESTAMP = re.compile(
    r"^(?P<whole>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?P<fraction>\.\d{1,6})?Z$"
)

_TARGET_FIELDS = {
    "version",
    "kind",
    "repo",
    "number",
    "state",
    "merged_at",
    "base_sha",
    "head_sha",
    "head_repo_id",
    "head_repo",
    "head_ref",
    "merge_base_sha",
    "changed_files",
    "diff_sha256",
    "diff_bytes",
    "trusted_context_sha256",
    "trusted_context_bytes",
    "object_bundle_sha256",
    "object_bundle_bytes",
}
_COMMON_EXPECTED_FIELDS = {
    "signerId",
    "executorId",
    "sessionId",
    "challenge",
    "target",
    "action",
    "job",
    "inputTree",
    "sandbox",
}
_EXECUTION_EXPECTED_FIELDS = _COMMON_EXPECTED_FIELDS | {
    "outputTree",
    "outputBundle",
    "stdout",
    "stderr",
    "exitCode",
}


class ReceiptError(ValueError):
    """The envelope is malformed, untrusted, stale, or contextually mismatched."""


@dataclass(frozen=True)
class VerifiedAttestation:
    signer_id: str
    payload: bytes
    payload_sha256: str


def _duplicate_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ReceiptError(f"duplicate JSON field {key!r}")
        value[key] = item
    return value


def _non_finite(value):
    raise ReceiptError(f"JSON number {value} is not finite")


def _unicode_scalars(value, label):
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ReceiptError(f"{label} contains an unpaired surrogate") from error
    elif isinstance(value, list):
        for item in value:
            _unicode_scalars(item, label)
    elif isinstance(value, dict):
        for key, item in value.items():
            _unicode_scalars(key, label)
            _unicode_scalars(item, label)


def _load_json(data, label, maximum):
    if not isinstance(data, bytes):
        raise ReceiptError(f"{label} must be bytes")
    if len(data) > maximum:
        raise ReceiptError(f"{label} exceeds {maximum} bytes")
    if data.startswith(b"\xef\xbb\xbf"):
        raise ReceiptError(f"{label} must not start with a UTF-8 BOM")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReceiptError(f"{label} is not valid UTF-8") from error
    try:
        value = json.loads(
            text,
            object_pairs_hook=_duplicate_object,
            parse_constant=_non_finite,
        )
    except ReceiptError:
        raise
    except (json.JSONDecodeError, RecursionError) as error:
        raise ReceiptError(f"{label} is not one complete JSON value") from error
    try:
        _unicode_scalars(value, label)
    except RecursionError as error:
        raise ReceiptError(f"{label} is nested too deeply") from error
    return value


def _canonical_value(value, label="canonical JSON"):
    if value is None or isinstance(value, (str, bool)):
        _unicode_scalars(value, label)
        return
    if type(value) is int:
        return
    if isinstance(value, list):
        for item in value:
            _canonical_value(item, label)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ReceiptError(f"{label} object keys must be strings")
            _unicode_scalars(key, label)
            _canonical_value(item, label)
        return
    raise ReceiptError(f"{label} contains an unsupported value")


def canonical_sha256(value):
    """Hash the deterministic JSON encoding used by the v1 binding fields."""
    _canonical_value(value)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise ReceiptError("value cannot be encoded as canonical JSON") from error
    return hashlib.sha256(encoded).hexdigest()


def dsse_pae(payload_type, payload):
    """Return DSSE v1 pre-authentication encoding for exact payload bytes."""
    if not isinstance(payload_type, str):
        raise ReceiptError("DSSE payload type must be text")
    if not isinstance(payload, bytes):
        raise ReceiptError("DSSE payload must be bytes")
    try:
        encoded_type = payload_type.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ReceiptError(
            "DSSE payload type contains an unpaired surrogate"
        ) from error
    return b"".join((
        b"DSSEv1 ",
        str(len(encoded_type)).encode("ascii"),
        b" ",
        encoded_type,
        b" ",
        str(len(payload)).encode("ascii"),
        b" ",
        payload,
    ))


def _object(value, label, fields):
    if type(value) is not dict:
        raise ReceiptError(f"{label} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ReceiptError(f"{label} field names must be strings")
    missing = sorted(fields - set(value))
    unknown = sorted(set(value) - fields)
    if missing:
        raise ReceiptError(f"{label} is missing {', '.join(missing)}")
    if unknown:
        raise ReceiptError(f"{label} has unknown field {', '.join(unknown)}")
    return value


def _same_json(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(
            _same_json(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _same_json(a, b) for a, b in zip(left, right)
        )
    return left == right


def _match(value, expected, label):
    if not _same_json(value, expected):
        raise ReceiptError(f"{label} does not match expected context")


def _text(value, label, allow_empty=False):
    if type(value) is not str or (not allow_empty and not value):
        qualifier = "text" if allow_empty else "non-empty text"
        raise ReceiptError(f"{label} must be {qualifier}")
    _unicode_scalars(value, label)
    if "\x00" in value:
        raise ReceiptError(f"{label} must not contain NUL")
    return value


def _integer(value, label, minimum=0):
    if type(value) is not int or not minimum <= value <= MAX_JSON_INTEGER:
        raise ReceiptError(
            f"{label} must be an integer from {minimum} to {MAX_JSON_INTEGER}"
        )
    return value


def _sha256(value, label):
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise ReceiptError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _git_object(value, label, sha256_only=False):
    pattern = _SHA256 if sha256_only else _GIT_OBJECT
    if type(value) is not str or not pattern.fullmatch(value):
        kind = "64-character SHA-256" if sha256_only else "full Git object"
        raise ReceiptError(f"{label} must be a lowercase {kind} digest")
    return value


def _uuid(value, label):
    _text(value, label)
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise ReceiptError(f"{label} must be a canonical UUID") from error
    if str(parsed) != value:
        raise ReceiptError(f"{label} must be a canonical UUID")
    return value


def _timestamp(value, label):
    if type(value) is not str:
        raise ReceiptError(f"{label} must be an RFC 3339 UTC timestamp")
    match = _UTC_TIMESTAMP.fullmatch(value)
    if match is None:
        raise ReceiptError(f"{label} must be an RFC 3339 UTC timestamp ending in Z")
    format_string = (
        "%Y-%m-%dT%H:%M:%S.%f"
        if match.group("fraction")
        else "%Y-%m-%dT%H:%M:%S"
    )
    raw = match.group("whole") + (match.group("fraction") or "")
    try:
        return datetime.strptime(raw, format_string).replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise ReceiptError(f"{label} is not a real UTC timestamp") from error


def _now(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ReceiptError("now must be a timezone-aware datetime")
    offset = value.utcoffset()
    if offset is None:
        raise ReceiptError("now must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _decode_base64(value, label, maximum):
    if type(value) is not str or not value:
        raise ReceiptError(f"{label} must be non-empty canonical base64")
    try:
        encoded = value.encode("ascii")
        standard = bool(set(encoded) & set(b"+/"))
        urlsafe = bool(set(encoded) & set(b"-_"))
        if standard and urlsafe:
            raise ValueError("mixed base64 alphabets")
        altchars = b"-_" if urlsafe else None
        decoded = base64.b64decode(encoded, altchars=altchars, validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as error:
        raise ReceiptError(f"{label} must be canonical base64") from error
    canonical = (
        base64.urlsafe_b64encode(decoded)
        if urlsafe
        else base64.b64encode(decoded)
    ).decode("ascii")
    if canonical != value:
        raise ReceiptError(f"{label} must be canonical padded base64")
    if len(decoded) > maximum:
        raise ReceiptError(f"{label} exceeds {maximum} decoded bytes")
    if not decoded:
        raise ReceiptError(f"{label} must decode to non-empty bytes")
    return decoded


def _verified_envelope(envelope, expected_signer, verify_signature):
    document = _load_json(envelope, "DSSE envelope", MAX_ENVELOPE_BYTES)
    if not isinstance(document, dict):
        raise ReceiptError("DSSE envelope must be an object")
    for field in ("payloadType", "payload", "signatures"):
        if field not in document:
            raise ReceiptError(f"DSSE envelope is missing {field}")
    payload_type = document["payloadType"]
    if payload_type != DSSE_PAYLOAD_TYPE:
        raise ReceiptError("DSSE payloadType is not the supported in-toto media type")
    payload = _decode_base64(document["payload"], "DSSE payload", MAX_PAYLOAD_BYTES)
    signatures = document["signatures"]
    if not isinstance(signatures, list) or len(signatures) != 1:
        raise ReceiptError("Underwrite v1 requires exactly one DSSE signature")
    signature = signatures[0]
    if not isinstance(signature, dict):
        raise ReceiptError("DSSE signature must be an object")
    if "keyid" not in signature or "sig" not in signature:
        raise ReceiptError("DSSE signature requires keyid and sig")
    keyid = _text(signature["keyid"], "DSSE keyid")
    raw_signature = _decode_base64(
        signature["sig"], "DSSE signature", MAX_SIGNATURE_BYTES
    )
    if not callable(verify_signature):
        raise ReceiptError("verify_signature must be callable")
    try:
        signer_id = verify_signature(
            dsse_pae(payload_type, payload), keyid, raw_signature
        )
    except Exception as error:
        raise ReceiptError("DSSE signature verification failed") from error
    if type(signer_id) is not str or not signer_id:
        raise ReceiptError("signature verifier returned no authenticated signer")
    _text(signer_id, "authenticated signer")
    if signer_id != expected_signer:
        raise ReceiptError("authenticated signer does not match expected signer")
    statement = _load_json(payload, "signed in-toto payload", MAX_PAYLOAD_BYTES)
    if not isinstance(statement, dict):
        raise ReceiptError("signed in-toto payload must be an object")
    attestation = VerifiedAttestation(
        signer_id=signer_id,
        payload=payload,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
    )
    return attestation, statement


def _validate_target(target):
    _object(target, "expected target", _TARGET_FIELDS)
    if type(target["version"]) is not int or target["version"] != 1:
        raise ReceiptError("expected target version must be 1")
    if target["kind"] != "github_pr":
        raise ReceiptError("expected target kind must be github_pr")
    if (
        type(target["repo"]) is not str
        or not _REPOSITORY.fullmatch(target["repo"])
    ):
        raise ReceiptError("expected target repo must be owner/name")
    _integer(target["number"], "expected target number", 1)
    if target["state"] not in ("open", "closed"):
        raise ReceiptError("expected target state must be open or closed")
    merged_at = target["merged_at"]
    if merged_at is not None:
        _timestamp(merged_at, "expected target merged_at")
    for field in ("base_sha", "head_sha", "merge_base_sha"):
        _git_object(target[field], f"expected target {field}")
    head_repo = target["head_repo"]
    head_repo_id = target["head_repo_id"]
    if head_repo is None:
        if head_repo_id is not None:
            raise ReceiptError(
                "expected target head_repo_id must be null with head_repo"
            )
    else:
        if type(head_repo) is not str or not _REPOSITORY.fullmatch(head_repo):
            raise ReceiptError("expected target head_repo must be null or owner/name")
        _integer(head_repo_id, "expected target head_repo_id", 1)
    _text(target["head_ref"], "expected target head_ref")
    _integer(target["changed_files"], "expected target changed_files")
    for digest_field in (
        "diff_sha256",
        "trusted_context_sha256",
        "object_bundle_sha256",
    ):
        _sha256(target[digest_field], f"expected target {digest_field}")
    _integer(target["diff_bytes"], "expected target diff_bytes")
    _integer(
        target["trusted_context_bytes"], "expected target trusted_context_bytes", 1
    )
    _integer(target["object_bundle_bytes"], "expected target object_bundle_bytes", 1)
    canonical_sha256(target)


def _validate_action(action):
    _object(action, "expected action", {"seq", "beat", "attempt"})
    for field in ("seq", "beat", "attempt"):
        _integer(action[field], f"expected action {field}", 1)


def _relative_path(value, label):
    _text(value, label)
    if value == ".":
        return
    if (
        value.startswith("/")
        or value.endswith("/")
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ReceiptError(f"{label} must be a normalized relative POSIX path")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ReceiptError(f"{label} must be a normalized relative POSIX path")


def _absolute_path(value, label):
    _text(value, label)
    if (
        not value.startswith("/")
        or value == "/"
        or value.endswith("/")
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ReceiptError(f"{label} must be a normalized absolute POSIX path")
    if any(part in ("", ".", "..") for part in value[1:].split("/")):
        raise ReceiptError(f"{label} must be a normalized absolute POSIX path")


def _validate_job(job):
    _object(
        job,
        "expected job",
        {"argv", "cwd", "environment", "executable", "stdin"},
    )
    argv = job["argv"]
    if type(argv) is not list or not argv:
        raise ReceiptError("expected job argv must be a non-empty array")
    for index, argument in enumerate(argv):
        _text(argument, f"expected job argv[{index}]", allow_empty=index != 0)
    _relative_path(job["cwd"], "expected job cwd")
    environment = job["environment"]
    if type(environment) is not dict:
        raise ReceiptError("expected job environment must be an object")
    for name, value in environment.items():
        _text(name, "expected environment name")
        if "=" in name:
            raise ReceiptError("expected environment names must not contain equals")
        _text(value, f"expected environment {name}", allow_empty=True)
    executable = _object(
        job["executable"],
        "expected job executable",
        {"path", "sha256", "bytes"},
    )
    _absolute_path(executable["path"], "expected job executable path")
    _sha256(executable["sha256"], "expected job executable sha256")
    _integer(executable["bytes"], "expected job executable bytes", 1)
    if argv[0] != executable["path"]:
        raise ReceiptError("expected job argv[0] must equal executable path")
    if job["stdin"] != "closed":
        raise ReceiptError("expected job stdin must be closed")
    canonical_sha256(job)


def _validate_sandbox(sandbox):
    _object(
        sandbox,
        "expected sandbox",
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
    )
    required = {
        "policy": SANDBOX_POLICY_TYPE,
        "credentials": "absent",
        "network": "denied",
        "hostWrites": "denied",
        "gitHooks": "disabled",
        "gitFilters": "disabled",
        "timeout": "enforced",
    }
    for field, value in required.items():
        if sandbox[field] != value:
            raise ReceiptError(f"expected sandbox {field} must be {value}")
    limits = _object(
        sandbox["limits"],
        "expected sandbox limits",
        {
            "wallSeconds",
            "cpuSeconds",
            "memoryBytes",
            "processes",
            "workspaceBytes",
            "outputBytes",
        },
    )
    for field, value in limits.items():
        _integer(value, f"expected sandbox limit {field}", 1)


def _validate_stream(stream, label):
    _object(stream, label, {"sha256", "bytes", "truncated"})
    _sha256(stream["sha256"], f"{label} sha256")
    _integer(stream["bytes"], f"{label} bytes")
    if stream["truncated"] is not False:
        raise ReceiptError(f"{label} must not be truncated")


def _expected(expected, execution):
    fields = _EXECUTION_EXPECTED_FIELDS if execution else _COMMON_EXPECTED_FIELDS
    _object(expected, "expected context", fields)
    _text(expected["signerId"], "expected signerId")
    _text(expected["executorId"], "expected executorId")
    _uuid(expected["sessionId"], "expected sessionId")
    _sha256(expected["challenge"], "expected challenge")
    _validate_target(expected["target"])
    _validate_action(expected["action"])
    _validate_job(expected["job"])
    _git_object(expected["inputTree"], "expected inputTree", sha256_only=True)
    _validate_sandbox(expected["sandbox"])
    if execution:
        _git_object(expected["outputTree"], "expected outputTree", sha256_only=True)
        bundle = _object(
            expected["outputBundle"],
            "expected outputBundle",
            {"sha256", "bytes"},
        )
        _sha256(bundle["sha256"], "expected outputBundle sha256")
        _integer(bundle["bytes"], "expected outputBundle bytes", 1)
        _validate_stream(expected["stdout"], "expected stdout")
        _validate_stream(expected["stderr"], "expected stderr")
        if (
            expected["stdout"]["bytes"] + expected["stderr"]["bytes"]
            > expected["sandbox"]["limits"]["outputBytes"]
        ):
            raise ReceiptError("expected streams exceed the sandbox output limit")
        _integer(expected["exitCode"], "expected exitCode")
    return copy.deepcopy(expected)


def _invocation(expected):
    target = expected["target"]
    return {
        "session": {
            "id": expected["sessionId"],
            "challenge": expected["challenge"],
        },
        "target": {
            "document": target,
            "digest": {"sha256": canonical_sha256(target)},
        },
        "sourceBundle": {
            "sha256": target["object_bundle_sha256"],
            "bytes": target["object_bundle_bytes"],
        },
        "action": expected["action"],
        "job": expected["job"],
        "jobDigest": {"sha256": canonical_sha256(expected["job"])},
        "inputTree": {"gitTree": expected["inputTree"]},
        "sandbox": expected["sandbox"],
    }


def _statement(statement, predicate_type):
    statement = _object(
        statement,
        "in-toto Statement",
        {"_type", "subject", "predicateType", "predicate"},
    )
    if statement["_type"] != STATEMENT_TYPE:
        raise ReceiptError("in-toto Statement has the wrong _type")
    if statement["predicateType"] != predicate_type:
        raise ReceiptError("in-toto Statement has the wrong predicateType")
    if not isinstance(statement["predicate"], dict):
        raise ReceiptError("in-toto Statement predicate must be an object")
    return statement


def _subject(value, name, tree):
    expected = [{"name": name, "digest": {"gitTree": tree}}]
    if not _same_json(value, expected):
        raise ReceiptError("in-toto Statement subject does not match expected tree")


def _capability(envelope, expected, verify_signature):
    attestation, raw_statement = _verified_envelope(
        envelope, expected["signerId"], verify_signature
    )
    statement = _statement(raw_statement, CAPABILITY_PREDICATE_TYPE)
    _subject(statement["subject"], "underwrite-execution-input", expected["inputTree"])
    predicate = _object(
        statement["predicate"],
        "host capability predicate",
        {"executor", "issuedAt", "expiresAt", "invocation"},
    )
    _match(
        predicate["executor"],
        {"id": expected["executorId"]},
        "host capability executor",
    )
    _match(
        predicate["invocation"],
        _invocation(expected),
        "host capability invocation",
    )
    issued_at = _timestamp(predicate["issuedAt"], "host capability issuedAt")
    expires_at = _timestamp(predicate["expiresAt"], "host capability expiresAt")
    duration = (expires_at - issued_at).total_seconds()
    if duration <= 0 or duration > MAX_CAPABILITY_SECONDS:
        raise ReceiptError(
            "host capability lifetime must be positive and at most 300 seconds"
        )
    return attestation, issued_at, expires_at


def verify_host_capability(envelope, expected, verify_signature, now):
    """Authenticate one fresh capability against caller-owned expected context."""
    expected = _expected(expected, execution=False)
    now = _now(now)
    attestation, issued_at, expires_at = _capability(
        envelope, expected, verify_signature
    )
    if now < issued_at or now >= expires_at:
        raise ReceiptError("host capability is not currently valid")
    return attestation


def verify_execution_receipt(
    capability_envelope,
    receipt_envelope,
    expected,
    verify_signature,
    now,
):
    """Authenticate a linked, complete receipt against independent expectations."""
    expected = _expected(expected, execution=True)
    now = _now(now)
    common = {field: expected[field] for field in _COMMON_EXPECTED_FIELDS}
    capability, issued_at, expires_at = _capability(
        capability_envelope, common, verify_signature
    )
    receipt, raw_statement = _verified_envelope(
        receipt_envelope, expected["signerId"], verify_signature
    )
    statement = _statement(raw_statement, EXECUTION_PREDICATE_TYPE)
    _subject(
        statement["subject"],
        "underwrite-execution-output",
        expected["outputTree"],
    )
    predicate = _object(
        statement["predicate"],
        "execution receipt predicate",
        {
            "executor",
            "capability",
            "startedAt",
            "finishedAt",
            "invocation",
            "outputTree",
            "outputBundle",
            "result",
            "streams",
        },
    )
    _match(
        predicate["executor"],
        {"id": expected["executorId"]},
        "execution receipt executor",
    )
    _match(
        predicate["capability"],
        {"payloadSha256": capability.payload_sha256},
        "execution receipt capability",
    )
    _match(
        predicate["invocation"],
        _invocation(expected),
        "execution receipt invocation",
    )
    output_tree = {"gitTree": expected["outputTree"]}
    _match(predicate["outputTree"], output_tree, "execution receipt outputTree")
    expected_bundle = {
        "gitTree": expected["outputTree"],
        "sha256": expected["outputBundle"]["sha256"],
        "bytes": expected["outputBundle"]["bytes"],
    }
    _match(
        predicate["outputBundle"],
        expected_bundle,
        "execution receipt outputBundle",
    )
    expected_result = {
        "status": "exited",
        "exitCode": expected["exitCode"],
        "signal": None,
        "timedOut": False,
        "resourceViolation": None,
        "isolationViolation": None,
        "survivingProcesses": 0,
        "teardown": "complete",
    }
    _match(
        predicate["result"],
        expected_result,
        "execution receipt complete result",
    )
    expected_streams = {
        "stdout": expected["stdout"],
        "stderr": expected["stderr"],
    }
    _match(predicate["streams"], expected_streams, "execution receipt streams")
    started_at = _timestamp(predicate["startedAt"], "execution receipt startedAt")
    finished_at = _timestamp(predicate["finishedAt"], "execution receipt finishedAt")
    if started_at < issued_at or started_at >= expires_at:
        raise ReceiptError("execution did not start within the capability lifetime")
    if finished_at < started_at:
        raise ReceiptError("execution receipt finished before it started")
    if finished_at > now:
        raise ReceiptError("execution receipt finished in the future")
    wall_seconds = expected["sandbox"]["limits"]["wallSeconds"]
    if (finished_at - started_at).total_seconds() > wall_seconds:
        raise ReceiptError("execution exceeded the attested wall-clock limit")
    return receipt
