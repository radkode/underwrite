#!/usr/bin/env python3
"""Bridge one frozen PR finding through attested execution to a local commit."""

import copy
import hashlib
import json
import os
import re
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway import artifacts  # noqa: E402
from gateway.signing import OpenSSLVerifier  # noqa: E402
from execution_receipt import (  # noqa: E402
    MAX_JSON_INTEGER,
    validate_execution_profile,
    verify_execution_receipt,
)
from pr_snapshot import TargetMoved, check, load_pr  # noqa: E402
from session_store import (  # noqa: E402
    MAX_IMPLEMENTATION_PROFILE_BYTES,
    MAX_OBJECT_BUNDLE_MEMORY_BYTES,
    Conflict,
    SessionStore,
    StoreError,
)


_ACTION_SEQ = 1
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_ZERO_SHA = "0" * 40
_PROFILE_FIELDS = {
    "version",
    "keyId",
    "signerId",
    "executorId",
    "job",
    "sandbox",
    "exitCode",
}
_EVIDENCE_FILES = {
    "request.json",
    "capability.dsse.json",
    "receipt.dsse.json",
    "output.bundle",
    "stdout",
    "stderr",
}
_GIT_SECONDS = 30
_LAND_SECONDS = 180
_JSON_BYTES = 1_000_000
_MAX_CONSUMER_DIFF_BYTES = 16 * 1024 * 1024
_MAX_CONSUMER_WORKSPACE_BYTES = 64 * 1024 * 1024
_MAX_CONSUMER_OUTPUT_BYTES = 16 * 1024 * 1024
_MIN_CONSUMER_MEMORY_BYTES = 64 * 1024 * 1024
_MAX_CONSUMER_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
_MAX_CONSUMER_SECONDS = 900
_MAX_LAND_ENTRIES = 20_000


class ImplementationError(StoreError):
    """The linked implementation cannot proceed safely."""


def _canonical_bytes(value):
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as error:
        raise ImplementationError("implementation metadata is not canonical JSON") from error


def _unique_object(pairs):
    value = {}
    for name, item in pairs:
        if name in value:
            raise ImplementationError(f"JSON object repeats field {name!r}")
        value[name] = item
    return value


def _bounded_json_integer(raw):
    digits = raw[1:] if raw.startswith("-") else raw
    if len(digits) > len(str(MAX_JSON_INTEGER)):
        raise ValueError("JSON integer exceeds the supported range")
    value = int(raw)
    if not -MAX_JSON_INTEGER <= value <= MAX_JSON_INTEGER:
        raise ValueError("JSON integer exceeds the supported range")
    return value


def _read_file(path, label, maximum_bytes):
    path = Path(path)
    if not hasattr(os, "O_NOFOLLOW"):
        raise ImplementationError("evidence reads require no-follow file opens")
    try:
        descriptor = os.open(
            os.fsencode(path),
            os.O_RDONLY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOCTTY", 0),
        )
    except OSError as error:
        raise ImplementationError(f"{label} cannot be opened") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ImplementationError(f"{label} must be one private regular file")
        if before.st_size < 0 or before.st_size > maximum_bytes:
            raise ImplementationError(f"{label} exceeds its byte limit")
        data = bytearray()
        while len(data) <= maximum_bytes:
            block = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1 - len(data)))
            if not block:
                break
            data.extend(block)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if identity(before) != identity(after) or len(data) != before.st_size:
        raise ImplementationError(f"{label} changed while it was read")
    return bytes(data)


def _read_json(source, label):
    if isinstance(source, dict):
        try:
            return copy.deepcopy(source)
        except RecursionError as error:
            raise ImplementationError(f"{label} is nested too deeply") from error
    raw = _read_file(source, label, _JSON_BYTES)
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_int=_bounded_json_integer,
        )
    except ImplementationError:
        raise
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ImplementationError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ImplementationError(f"{label} must be a JSON object")
    return value


def _profile(source):
    profile = _read_json(source, "trusted profile")
    if set(profile) != _PROFILE_FIELDS or profile.get("version") != 1:
        raise ImplementationError(
            "trusted profile must be version 1 with the exact supported fields"
        )
    profile = validate_execution_profile(profile)
    limits = profile["sandbox"]["limits"]
    if limits["workspaceBytes"] > _MAX_CONSUMER_WORKSPACE_BYTES:
        raise ImplementationError("trusted profile workspace limit is too large")
    if limits["outputBytes"] > _MAX_CONSUMER_OUTPUT_BYTES:
        raise ImplementationError("trusted profile output limit is too large")
    if limits["processes"] != 1:
        raise ImplementationError("trusted profile process limit must be 1")
    if not (
        _MIN_CONSUMER_MEMORY_BYTES
        <= limits["memoryBytes"]
        <= _MAX_CONSUMER_MEMORY_BYTES
    ):
        raise ImplementationError("trusted profile memory limit is unsupported")
    if (
        limits["wallSeconds"] > _MAX_CONSUMER_SECONDS
        or limits["cpuSeconds"] > _MAX_CONSUMER_SECONDS
        or limits["cpuSeconds"] > limits["wallSeconds"]
    ):
        raise ImplementationError("trusted profile time limits are unsupported")
    if len(_canonical_bytes(profile)) > MAX_IMPLEMENTATION_PROFILE_BYTES:
        raise ImplementationError("trusted profile exceeds the implementation byte limit")
    return profile


def _git_environment(extra=None):
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    environment.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
    })
    if extra:
        environment.update(extra)
    return environment


def _git_result(
    repo,
    command,
    *,
    input_bytes=None,
    extra_environment=None,
    deadline=None,
):
    timeout = _GIT_SECONDS
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ImplementationError("implementation landing timed out")
        timeout = min(timeout, remaining)
    invocation = [
        "git",
        "--no-replace-objects",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.fsync=objects,pack-metadata,reference",
        "-c",
        "core.fsyncMethod=fsync",
        "-c",
        "core.pager=cat",
        "-c",
        "commit.gpgSign=false",
        "-C",
        str(repo),
        *command,
    ]
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as diagnostics:
        try:
            process = subprocess.Popen(
                invocation,
                stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                stdout=output,
                stderr=diagnostics,
                env=_git_environment(extra_environment),
                start_new_session=True,
            )
        except OSError as error:
            raise ImplementationError("Git could not be started") from error
        try:
            process.communicate(input=input_bytes, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise ImplementationError("Git operation timed out") from error
        output_size = os.fstat(output.fileno()).st_size
        diagnostic_size = os.fstat(diagnostics.fileno()).st_size
        if output_size > 16 * 1024 * 1024 or diagnostic_size > 1024 * 1024:
            raise ImplementationError("Git operation exceeded its output limit")
        output.seek(0)
        diagnostics.seek(0)
        raw_output = output.read()
        raw_diagnostics = diagnostics.read()
    return subprocess.CompletedProcess(
        invocation,
        process.returncode,
        raw_output,
        raw_diagnostics,
    )


def _git(
    repo,
    command,
    *,
    input_bytes=None,
    extra_environment=None,
    deadline=None,
):
    result = _git_result(
        repo,
        command,
        input_bytes=input_bytes,
        extra_environment=extra_environment,
        deadline=deadline,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise ImplementationError(f"Git failed: {detail or result.returncode}")
    return result.stdout


def _repository(repo_root, deadline=None):
    repo = Path(repo_root).expanduser().resolve()
    raw_version = _git(repo, ["version"], deadline=deadline)
    try:
        version_text = raw_version.decode("ascii").strip()
    except UnicodeError as error:
        raise ImplementationError("Git returned a non-ASCII version") from error
    version_match = re.match(r"^git version (\d+)\.(\d+)(?:\.|$)", version_text)
    if version_match is None or tuple(map(int, version_match.groups())) < (2, 36):
        raise ImplementationError("implementation landing requires Git 2.36 or newer")
    top = _git(repo, ["rev-parse", "--show-toplevel"], deadline=deadline)
    try:
        top = Path(top.decode("utf-8").strip()).resolve()
    except UnicodeError as error:
        raise ImplementationError("Git returned a non-UTF-8 repository path") from error
    if top != repo:
        raise ImplementationError("repo root must name the exact Git worktree root")
    if _git(
        repo, ["rev-parse", "--is-bare-repository"], deadline=deadline
    ).strip() != b"false":
        raise ImplementationError("implementation repository must be a worktree")
    if _git(
        repo, ["rev-parse", "--show-object-format"], deadline=deadline
    ).strip() != b"sha1":
        raise ImplementationError("implementation repository must use SHA-1 objects")
    return repo


def _ref(repo, ref, deadline=None):
    result = _git_result(
        repo,
        ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        deadline=deadline,
    )
    if result.returncode:
        return None
    try:
        commit = result.stdout.decode("ascii").strip()
    except UnicodeError as error:
        raise ImplementationError("Git returned a non-ASCII ref") from error
    if not _FULL_SHA.fullmatch(commit):
        raise ImplementationError("Git returned an invalid ref commit")
    return commit


def _update_ref(repo, ref, new, old, deadline=None):
    _git(
        repo,
        ["update-ref", "--no-deref", ref, new, old],
        deadline=deadline,
    )


def _checked_out(repo, ref, deadline=None):
    records = _git(
        repo,
        ["worktree", "list", "--porcelain", "-z"],
        deadline=deadline,
    ).split(b"\0")
    expected = b"branch " + ref.encode("utf-8")
    return any(
        line == expected
        for record in records
        for line in record.splitlines()
    )


def _check_source(source, api, deadline=None):
    if deadline is None:
        return check(source) if api is None else check(source, api=api)
    if api is None:
        def bounded_api(repo, number):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ImplementationError("implementation landing timed out")
            return load_pr(repo, number, timeout=remaining)

        result = check(source, api=bounded_api)
    else:
        _check_land_deadline(deadline)
        result = check(source, api=api)
    _check_land_deadline(deadline)
    return result


def _verify_link_bundle(source, target):
    bundle = source.read_object_bundle()
    with tempfile.TemporaryDirectory(prefix="underwrite-link-verify-") as temporary:
        artifacts.verify_source_bundle(
            bundle,
            target,
            Path(temporary) / "input",
            maximum_workspace_bytes=_MAX_CONSUMER_WORKSPACE_BYTES,
        )
    return bundle


def _seed_branch(source, child, repo, link, source_bundle, api):
    branch = link["branch"]
    ref = "refs/heads/" + branch
    if _git_result(repo, ["check-ref-format", "--branch", branch]).returncode:
        raise ImplementationError("generated implementation branch is invalid")
    target = source.verify_target_files()
    child_target = child.verify_target_files()
    if target != child_target:
        raise Conflict("linked child target does not match its source")
    with tempfile.TemporaryDirectory(prefix="underwrite-link-") as temporary:
        bundle = Path(temporary) / "pr.bundle"
        _write_exact(bundle, source_bundle)
        _git(repo, ["bundle", "unbundle", str(bundle)])
    if _ref(repo, target["head_sha"]) != target["head_sha"]:
        raise ImplementationError("frozen PR head was not imported into the repository")
    current = _ref(repo, ref)
    if current is None:
        _check_source(source, api)
        if _checked_out(repo, ref):
            raise Conflict("implementation branch is checked out in a worktree")
        _update_ref(repo, ref, target["head_sha"], _ZERO_SHA)
        current = target["head_sha"]
    if current == target["head_sha"]:
        return
    try:
        attempt = child.implementation_attempt(_ACTION_SEQ)
    except StoreError:
        attempt = None
    plan = attempt.get("commit_plan") if isinstance(attempt, dict) else None
    if (
        isinstance(attempt, dict)
        and attempt.get("state") == "landed"
        and isinstance(plan, dict)
        and plan.get("commit") == current
    ):
        return
    raise Conflict("implementation branch no longer names its recorded position")


def _linked_context(child_root, source_root):
    source = SessionStore(source_root)
    child_path = Path(child_root).expanduser()
    for path, label in (
        (child_path.parent, "linked implementation parent"),
        (child_path, "linked implementation child"),
    ):
        try:
            details = path.lstat()
        except OSError as error:
            raise ImplementationError(f"{label} cannot be inspected") from error
        if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
            raise ImplementationError(f"{label} must be a real directory")
    link_id = child_path.name
    if not re.fullmatch(r"[0-9a-f]{64}", link_id):
        raise Conflict("child path does not name an implementation link")
    link = source.implementation_link(link_id)
    if link.get("state") != "ready":
        raise Conflict("source implementation link is not ready")
    expected_root = source.root / link["child_path"]
    if os.path.realpath(child_path) != os.path.realpath(expected_root):
        raise Conflict("child session is not at its source-bound path")
    for path, label in (
        (child_path / "session.sqlite3", "linked child database"),
        (child_path / ".session.lock", "linked child lock"),
    ):
        try:
            details = path.lstat()
        except OSError as error:
            raise ImplementationError(f"{label} cannot be inspected") from error
        if (
            not stat.S_ISREG(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or details.st_nlink != 1
        ):
            raise ImplementationError(f"{label} must be one private regular file")
    child = SessionStore(child_path)
    child_session = child.snapshot()[0]
    linked = child_session.get("linked_implementation")
    if not isinstance(linked, dict):
        raise Conflict("child session is not a linked implementation")
    if linked.get("link_id") != link_id:
        raise Conflict("linked implementation has no link identity")
    source_id = source.delivery_state()["session_id"]
    child_id = child.delivery_state()["session_id"]
    if (
        link.get("source_session_id") != source_id
        or link.get("child_session_id") != child_id
        or linked.get("source_session_id") != source_id
    ):
        raise Conflict("linked implementation session identities do not match")
    if child.verify_target_files() != source.verify_target_files():
        raise Conflict("linked child target does not match its source")
    if child_session.get("delivery_branch") != link.get("branch"):
        raise Conflict("linked child branch does not match its source authorization")
    return source, child, link


def link(source_root, repo_root, *, seq, beat, actor, approval, api=None):
    source = SessionStore(source_root)
    target = source.frozen_target()
    diff_bytes = target.get("diff_bytes")
    if (
        isinstance(diff_bytes, bool)
        or not isinstance(diff_bytes, int)
        or diff_bytes < 0
    ):
        raise Conflict("implementation requires a bounded frozen PR diff")
    if diff_bytes > _MAX_CONSUMER_DIFF_BYTES:
        raise ImplementationError("frozen source diff exceeds the consumer byte limit")
    bundle_bytes = target.get("object_bundle_bytes")
    if (
        isinstance(bundle_bytes, bool)
        or not isinstance(bundle_bytes, int)
        or bundle_bytes <= 0
    ):
        raise Conflict("implementation requires a frozen Git object bundle")
    if bundle_bytes > MAX_OBJECT_BUNDLE_MEMORY_BYTES:
        raise ImplementationError("frozen source bundle exceeds the gateway byte limit")
    repo = _repository(repo_root)
    target = source.verify_target_files()
    source_bundle = _verify_link_bundle(source, target)
    _check_source(source, api)
    authorization = source.authorize_implementation(seq, beat, actor, approval)
    created = source.create_linked_implementation(authorization["link_id"])
    child = SessionStore(created["child_root"])
    _seed_branch(
        source,
        child,
        repo,
        authorization,
        source_bundle,
        None if api is None else api,
    )
    ready = source.complete_implementation_link(
        authorization["link_id"], created["child_session_id"]
    )
    return {
        "link": ready,
        "child_root": created["child_root"],
        "child_session_id": created["child_session_id"],
        "action_seq": created["action_seq"],
        "beat": created["beat"],
        "branch": created["branch"],
    }


def request(child_root, source_root, profile_source, *, api=None):
    source, child, _link = _linked_context(child_root, source_root)
    _check_source(source, api)
    attempt = child.reserve_implementation_attempt(_ACTION_SEQ, _profile(profile_source))
    return copy.deepcopy(attempt["request"])


def _public_attempt(attempt):
    result = copy.deepcopy(attempt)
    result.pop("capability", None)
    result.pop("receipt", None)
    return result


def fail(child_root, source_root, *, attempt, reason):
    _source, child, _link = _linked_context(child_root, source_root)
    failed = child.fail_implementation_attempt(
        _ACTION_SEQ,
        attempt,
        reason,
    )
    return _public_attempt(failed)


def _stream_descriptor(data):
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "truncated": False,
    }


def _evidence_directory(path):
    path = Path(path)
    try:
        details = path.lstat()
    except OSError as error:
        raise ImplementationError("evidence directory cannot be inspected") from error
    if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise ImplementationError("evidence directory must be a real directory")
    _require_exact_directory_names(
        path,
        _EVIDENCE_FILES,
        "evidence directory",
        ImplementationError,
    )
    return path


def _require_exact_directory_names(path, expected, label, error_type):
    expected = set(expected)
    names = set()
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if len(names) == len(expected):
                    raise error_type(f"{label} does not have the exact file set")
                names.add(entry.name)
    except error_type:
        raise
    except OSError as error:
        raise error_type(f"{label} cannot be enumerated") from error
    if names != expected:
        raise error_type(f"{label} does not have the exact file set")
    return names


def _write_exact(path, data):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(os.fsencode(path), flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ImplementationError("could not persist complete execution evidence")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path):
    descriptor = os.open(os.fsencode(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stored_evidence_root(child, attempt):
    return child.root / "implementation-evidence" / str(attempt)


def _verify_persisted_evidence(child, attempt, values):
    destination = _stored_evidence_root(child, attempt)
    try:
        details = destination.lstat()
    except FileNotFoundError as error:
        raise Conflict("persisted implementation evidence is missing") from error
    if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise Conflict("stored implementation evidence is not a directory")
    _require_exact_directory_names(
        destination,
        values,
        "stored implementation evidence",
        Conflict,
    )
    for name, expected in values.items():
        actual = _read_file(destination / name, f"stored {name}", len(expected))
        if actual != expected:
            raise Conflict(f"stored implementation evidence {name} changed")
    return destination


def _persist_evidence(child, attempt, values):
    parent = child.root / "implementation-evidence"
    created = False
    try:
        parent.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        details = parent.lstat()
        if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
            raise ImplementationError("implementation evidence root is not a directory")
    if created:
        _fsync_directory(child.root)
    destination = _stored_evidence_root(child, attempt)

    if destination.exists():
        _verify_persisted_evidence(child, attempt, values)
        return destination
    temporary = Path(tempfile.mkdtemp(prefix=f".{attempt}.", dir=parent))
    try:
        for name, data in values.items():
            _write_exact(temporary / name, data)
        _fsync_directory(temporary)
        try:
            os.rename(temporary, destination)
        except OSError:
            if not destination.exists():
                raise
            _verify_persisted_evidence(child, attempt, values)
        _fsync_directory(parent)
        _fsync_directory(child.root)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return destination


def _verified_replay(child, attempt, values):
    if attempt.get("state") not in ("verified", "prepared", "landed"):
        return None
    evidence = attempt.get("evidence")
    if not isinstance(evidence, dict):
        raise Conflict("verified implementation attempt has no evidence")
    profile = attempt["trusted_profile"]
    checks = {
        "requestSha256": hashlib.sha256(values["request.json"]).hexdigest(),
        "capabilitySha256": hashlib.sha256(
            values["capability.dsse.json"]
        ).hexdigest(),
        "receiptSha256": hashlib.sha256(values["receipt.dsse.json"]).hexdigest(),
        "outputBundle": {
            "sha256": hashlib.sha256(values["output.bundle"]).hexdigest(),
            "bytes": len(values["output.bundle"]),
        },
        "stdout": _stream_descriptor(values["stdout"]),
        "stderr": _stream_descriptor(values["stderr"]),
        "keyId": profile["keyId"],
        "signerId": profile["signerId"],
        "executorId": profile["executorId"],
        "exitCode": profile["exitCode"],
    }
    for name, value in checks.items():
        if evidence.get(name) != value:
            raise Conflict(f"verified implementation replay changed {name}")
    if (
        attempt.get("capability") != values["capability.dsse.json"]
        or attempt.get("receipt") != values["receipt.dsse.json"]
    ):
        raise Conflict("verified implementation replay changed signed evidence")
    _verify_persisted_evidence(child, attempt["attempt"], values)
    return attempt


def consume(
    child_root,
    source_root,
    profile_source,
    public_key,
    evidence_dir,
    *,
    now=None,
):
    source, child, _link = _linked_context(child_root, source_root)
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
        raise ImplementationError("execution request is not valid UTF-8 JSON") from error
    try:
        attempt_number = request_value["action"]["attempt"]
    except (KeyError, TypeError) as error:
        raise ImplementationError("execution request has no attempt coordinate") from error
    attempt = child.implementation_attempt(_ACTION_SEQ, attempt_number)
    expected_request = _canonical_bytes(attempt["request"])
    if request_bytes != expected_request:
        raise Conflict("evidence request bytes do not match the reserved request")
    if attempt["trusted_profile"] != profile:
        raise Conflict("trusted execution profile changed after request reservation")
    workspace_limit = profile["sandbox"]["limits"]["workspaceBytes"]
    output_limit = profile["sandbox"]["limits"]["outputBytes"]
    bundle_limit = workspace_limit * 2
    capability = _read_file(
        directory / "capability.dsse.json", "host capability", _JSON_BYTES
    )
    receipt = _read_file(
        directory / "receipt.dsse.json", "execution receipt", _JSON_BYTES
    )
    output_bundle = _read_file(
        directory / "output.bundle", "output bundle", bundle_limit
    )
    stdout = _read_file(directory / "stdout", "execution stdout", output_limit)
    stderr = _read_file(directory / "stderr", "execution stderr", output_limit)
    if len(stdout) + len(stderr) > output_limit:
        raise ImplementationError("execution streams exceed the trusted output limit")
    verifier = OpenSSLVerifier(public_key, profile["signerId"])
    if verifier.key_id != profile["keyId"]:
        raise Conflict("public key does not match the trusted profile key ID")
    evidence_values = {
        "request.json": request_bytes,
        "capability.dsse.json": capability,
        "receipt.dsse.json": receipt,
        "output.bundle": output_bundle,
        "stdout": stdout,
        "stderr": stderr,
    }
    replay = _verified_replay(child, attempt, evidence_values)
    if replay is not None:
        return _public_attempt(replay)
    target = source.verify_target_files()
    if target != child.verify_target_files() or target != attempt["request"]["target"]:
        raise Conflict("execution request target is not the frozen linked target")
    source_bundle = source.read_object_bundle()
    with tempfile.TemporaryDirectory(prefix="underwrite-consume-") as temporary:
        input_result = artifacts.verify_source_bundle(
            source_bundle,
            target,
            Path(temporary) / "input",
            maximum_workspace_bytes=workspace_limit,
        )
    output_descriptor = artifacts.verify_output_bundle(
        output_bundle,
        maximum_workspace_bytes=workspace_limit,
        maximum_bundle_bytes=bundle_limit,
    )
    expected = {
        "signerId": profile["signerId"],
        "executorId": profile["executorId"],
        "sessionId": attempt["request"]["sessionId"],
        "challenge": attempt["challenge"],
        "target": target,
        "action": copy.deepcopy(attempt["request"]["action"]),
        "job": copy.deepcopy(profile["job"]),
        "inputTree": input_result.git_tree,
        "sandbox": copy.deepcopy(profile["sandbox"]),
        "outputTree": output_descriptor.git_tree,
        "outputBundle": {
            "sha256": output_descriptor.sha256,
            "bytes": output_descriptor.bytes,
        },
        "stdout": _stream_descriptor(stdout),
        "stderr": _stream_descriptor(stderr),
        "exitCode": profile["exitCode"],
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
        "outputTree": output_descriptor.git_tree,
        "outputBundle": {
            "sha256": output_descriptor.sha256,
            "bytes": output_descriptor.bytes,
        },
        "stdout": _stream_descriptor(stdout),
        "stderr": _stream_descriptor(stderr),
        "exitCode": profile["exitCode"],
    }
    _persist_evidence(
        child,
        attempt_number,
        evidence_values,
    )
    return _public_attempt(
        child.record_verified_implementation(
            _ACTION_SEQ,
            attempt_number,
            capability,
            receipt,
            evidence,
        )
    )


def _check_land_deadline(deadline):
    if deadline - time.monotonic() <= 0:
        raise ImplementationError("implementation landing timed out")


def _materialized_file_bytes(path, expected, remaining):
    if not hasattr(os, "O_NOFOLLOW"):
        raise ImplementationError("landing requires no-follow file opens")
    descriptor = os.open(
        path,
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOCTTY", 0),
    )
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > remaining
        ):
            raise ImplementationError("materialized output file is not safely bounded")
        body = bytearray()
        while len(body) < before.st_size:
            block = os.read(descriptor, min(1024 * 1024, before.st_size - len(body)))
            if not block:
                raise ImplementationError("materialized output file became shorter")
            body.extend(block)
        if os.read(descriptor, 1):
            raise ImplementationError("materialized output file became longer")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if identity(expected) != identity(before) or identity(before) != identity(after):
        raise ImplementationError("materialized output file changed while read")
    return bytes(body)


def _write_transient(path, body):
    descriptor = os.open(
        os.fsencode(path),
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ImplementationError("could not stage materialized output")
            view = view[written:]
    finally:
        os.close(descriptor)


def _write_tree(repo, directory, maximum_workspace_bytes, deadline):
    root = os.fsencode(directory)
    used = 0
    visited = 0
    entries = []
    with tempfile.TemporaryDirectory(prefix="underwrite-git-index-") as temporary:
        temporary_root = Path(temporary)
        inputs = temporary_root / "blobs"
        inputs.mkdir(mode=0o700)

        def walk(parent, prefix):
            nonlocal used, visited
            _check_land_deadline(deadline)
            try:
                names = sorted(os.listdir(parent))
            except OSError as error:
                raise ImplementationError(
                    "materialized output cannot be enumerated"
                ) from error
            if not names:
                raise ImplementationError("materialized output contains an empty directory")
            for name in names:
                _check_land_deadline(deadline)
                visited += 1
                if visited > _MAX_LAND_ENTRIES:
                    raise ImplementationError("materialized output has too many entries")
                path = os.path.join(parent, name)
                relative = prefix + name
                try:
                    details = os.lstat(path)
                except OSError as error:
                    raise ImplementationError(
                        "materialized output entry cannot be inspected"
                    ) from error
                if stat.S_ISDIR(details.st_mode):
                    walk(path, relative + b"/")
                    continue
                if stat.S_ISREG(details.st_mode):
                    body = _materialized_file_bytes(
                        path, details, maximum_workspace_bytes - used
                    )
                    mode = b"100755" if details.st_mode & 0o111 else b"100644"
                elif stat.S_ISLNK(details.st_mode):
                    try:
                        body = os.readlink(path)
                    except OSError as error:
                        raise ImplementationError(
                            "materialized output link cannot be read"
                        ) from error
                    if isinstance(body, str):
                        body = os.fsencode(body)
                    if len(body) > maximum_workspace_bytes - used:
                        raise ImplementationError("materialized output exceeds its byte limit")
                    mode = b"120000"
                else:
                    raise ImplementationError(
                        "materialized output contains an unsupported file"
                    )
                used += len(body)
                blob_path = inputs / str(len(entries))
                _write_transient(blob_path, body)
                entries.append((relative, mode, blob_path))

        walk(root, b"")
        hash_paths = [os.fsencode(path) for _name, _mode, path in entries]
        if any(b"\n" in path for path in hash_paths):
            raise ImplementationError("temporary Git input path contains a newline")
        raw_ids = _git(
            repo,
            ["hash-object", "-w", "--stdin-paths", "--no-filters"],
            input_bytes=b"".join(path + b"\n" for path in hash_paths),
            deadline=deadline,
        ).splitlines()
        try:
            decoded_ids = [object_id.decode("ascii") for object_id in raw_ids]
        except UnicodeError as error:
            raise ImplementationError("Git returned non-ASCII batched blob IDs") from error
        if len(raw_ids) != len(entries) or any(
            not _FULL_SHA.fullmatch(object_id) for object_id in decoded_ids
        ):
            raise ImplementationError("Git returned invalid batched blob IDs")

        index = temporary_root / "index"
        environment = {"GIT_INDEX_FILE": str(index)}
        _git(repo, ["read-tree", "--empty"], extra_environment=environment, deadline=deadline)
        index_payload = b"".join(
            mode + b" " + object_id + b"\t" + name + b"\0"
            for (name, mode, _path), object_id in zip(entries, raw_ids)
        )
        _git(
            repo,
            ["update-index", "-z", "--index-info"],
            input_bytes=index_payload,
            extra_environment=environment,
            deadline=deadline,
        )
        raw_tree = _git(
            repo,
            ["write-tree"],
            extra_environment=environment,
            deadline=deadline,
        )
    try:
        tree = raw_tree.decode("ascii").strip()
    except UnicodeError as error:
        raise ImplementationError("Git returned a non-ASCII tree ID") from error
    if not _FULL_SHA.fullmatch(tree):
        raise ImplementationError("Git returned an invalid tree ID")
    return tree


def _commit(repo, tree, parent, message, deadline):
    if not isinstance(message, str) or not message.strip() or "\x00" in message:
        raise ImplementationError("commit message must be non-empty text without NUL")
    body = (message.strip() + "\n").encode("utf-8")
    timestamp = str(int(time.time())) + " +0000"
    environment = {
        "GIT_AUTHOR_NAME": "Underwrite",
        "GIT_AUTHOR_EMAIL": "underwrite@underwrite.invalid",
        "GIT_AUTHOR_DATE": timestamp,
        "GIT_COMMITTER_NAME": "Underwrite",
        "GIT_COMMITTER_EMAIL": "underwrite@underwrite.invalid",
        "GIT_COMMITTER_DATE": timestamp,
    }
    raw = _git(
        repo,
        ["commit-tree", tree, "-p", parent],
        input_bytes=body,
        extra_environment=environment,
        deadline=deadline,
    )
    try:
        commit = raw.decode("ascii").strip()
    except UnicodeError as error:
        raise ImplementationError("Git returned a non-ASCII commit ID") from error
    if not _FULL_SHA.fullmatch(commit):
        raise ImplementationError("Git returned an invalid commit ID")
    _verify_commit(repo, commit, tree, parent, body, deadline)
    return commit


def _verify_commit(repo, commit, tree, parent, message, deadline):
    raw = _git(repo, ["cat-file", "-p", commit], deadline=deadline)
    try:
        headers, body = raw.split(b"\n\n", 1)
    except ValueError as error:
        raise ImplementationError("planned commit is malformed") from error
    lines = headers.splitlines()
    if not lines or lines[0] != b"tree " + tree.encode("ascii"):
        raise Conflict("planned commit tree changed")
    parents = [line[7:] for line in lines if line.startswith(b"parent ")]
    if parents != [parent.encode("ascii")]:
        raise Conflict("planned commit does not have exactly the frozen head as parent")
    if body != message:
        raise Conflict("planned commit message does not match the requested message")


def _plan_commit(child, repo, attempt, message, deadline):
    evidence = attempt.get("evidence")
    if not isinstance(evidence, dict):
        raise Conflict("implementation attempt has no verified evidence")
    profile = attempt["trusted_profile"]
    workspace_limit = profile["sandbox"]["limits"]["workspaceBytes"]
    bundle_limit = workspace_limit * 2
    bundle_value = evidence["outputBundle"]
    descriptor = artifacts.ArtifactDescriptor(
        evidence["outputTree"], bundle_value["sha256"], bundle_value["bytes"]
    )
    bundle = _read_file(
        _stored_evidence_root(child, attempt["attempt"]) / "output.bundle",
        "stored output bundle",
        bundle_limit,
    )
    with tempfile.TemporaryDirectory(prefix="underwrite-land-") as temporary:
        output = Path(temporary) / "output"
        artifacts.materialize_output_bundle(
            bundle,
            output,
            maximum_workspace_bytes=workspace_limit,
            maximum_bundle_bytes=bundle_limit,
            expected=descriptor,
            deadline=deadline,
        )
        tree = _write_tree(repo, output, workspace_limit, deadline)
    parent = attempt["request"]["target"]["head_sha"]
    commit = _commit(repo, tree, parent, message, deadline)
    measured = artifacts.measure_commit_tree(
        repo,
        commit,
        maximum_workspace_bytes=workspace_limit,
        deadline=deadline,
    )
    if measured != evidence["outputTree"]:
        raise Conflict("planned commit content does not match the signed output tree")
    session = child.snapshot()[0]
    return {
        "version": 1,
        "commit": commit,
        "parent": parent,
        "tree": tree,
        "outputTree": measured,
        "branch": session["delivery_branch"],
    }


def _post_move(receipt, source, api, deadline):
    result = copy.deepcopy(receipt)
    try:
        _check_source(source, api, deadline)
    except TargetMoved as error:
        result["replacement_required"] = True
        result["replacement_reason"] = str(error)
    else:
        result["replacement_required"] = False
    return result


def land(child_root, source_root, repo_root, *, attempt, message, api=None):
    if not isinstance(message, str) or not message.strip() or "\x00" in message:
        raise ImplementationError("commit message must be non-empty text without NUL")
    deadline = time.monotonic() + _LAND_SECONDS
    source, child, link = _linked_context(child_root, source_root)
    repo = _repository(repo_root, deadline)
    current_attempt = child.implementation_attempt(_ACTION_SEQ, attempt)
    state = current_attempt["state"]
    if state == "landed":
        plan = current_attempt["commit_plan"]
    elif state == "prepared":
        plan = current_attempt["commit_plan"]
    elif state == "verified":
        plan = _plan_commit(child, repo, current_attempt, message, deadline)
        current_attempt = child.prepare_implementation_land(
            _ACTION_SEQ, attempt, plan
        )
        plan = current_attempt["commit_plan"]
    else:
        raise Conflict(f"implementation attempt is {state}, not verified")
    if plan.get("branch") != link.get("branch"):
        raise Conflict("planned commit branch does not match the linked branch")
    expected_message = (message.strip() + "\n").encode("utf-8")
    _verify_commit(
        repo,
        plan["commit"],
        plan["tree"],
        plan["parent"],
        expected_message,
        deadline,
    )
    measured = artifacts.measure_commit_tree(
        repo,
        plan["commit"],
        maximum_workspace_bytes=current_attempt["trusted_profile"]["sandbox"]["limits"][
            "workspaceBytes"
        ],
        deadline=deadline,
    )
    if measured != plan["outputTree"]:
        raise Conflict("planned commit no longer matches the signed output tree")
    ref = "refs/heads/" + plan["branch"]
    current = _ref(repo, ref, deadline)
    if current == plan["commit"]:
        receipt = child.finish_implementation_land(
            _ACTION_SEQ, attempt, plan["commit"], plan["branch"]
        )
        return _post_move(receipt, source, api, deadline)
    if state == "landed":
        raise Conflict("landed implementation branch no longer names its commit")
    if current != plan["parent"]:
        raise Conflict("implementation branch moved from its frozen parent")
    _check_source(source, api, deadline)
    if _checked_out(repo, ref, deadline):
        raise Conflict("implementation branch is checked out in a worktree")
    _update_ref(repo, ref, plan["commit"], plan["parent"], deadline)
    receipt = child.finish_implementation_land(
        _ACTION_SEQ, attempt, plan["commit"], plan["branch"]
    )
    return _post_move(receipt, source, api, deadline)
