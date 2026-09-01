#!/usr/bin/env python3
"""Run one trusted gateway request and publish its consumer evidence."""

import argparse
import fcntl
import hashlib
import json
import os
import posixpath
import sqlite3
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.artifacts import ArtifactError  # noqa: E402
from gateway.broker import (  # noqa: E402
    AttemptFailed,
    ExecutionBroker,
    ExecutionRequest,
    GatewayError,
    GatewayPolicy,
)
from gateway.docker_runner import DockerError, DockerRunner  # noqa: E402
from gateway.signing import OpenSSLSigner, SigningError  # noqa: E402
from gateway.store import ReplayConflict, StoreError, StoredExecution  # noqa: E402
from skills.underwrite.scripts.execution_receipt import (  # noqa: E402
    MAX_JSON_INTEGER,
    ReceiptError,
    validate_execution_profile,
)


class AdapterError(RuntimeError):
    """The supervised gateway handoff cannot be completed safely."""


_CONFIG_FIELDS = {
    "version",
    "privateKey",
    "publicKey",
    "storeRoot",
    "docker",
    "openssl",
    "git",
    "deploymentId",
    "runtimeDomainId",
    "dockerHost",
    "image",
    "platform",
    "runnerSha256",
    "targetUid",
    "targetGid",
    "consumerGid",
    "capabilitySeconds",
    "maxSourceBundleBytes",
    "profile",
}
_EVIDENCE_FILES = (
    "request.json",
    "capability.dsse.json",
    "receipt.dsse.json",
    "output.bundle",
    "stdout",
    "stderr",
)
_STORED_ARTIFACTS = {
    "sourceBundle",
    "outputBundle",
    "stdout",
    "stderr",
}
_MAX_CONFIG_BYTES = 1024 * 1024
_MAX_PROFILE_BYTES = 256 * 1024
_MAX_REQUEST_BYTES = 384 * 1024
_MAX_JSON_DEPTH = 32
_MAX_PATH_BYTES = 4095
_MAX_PATH_COMPONENT_BYTES = 255
_MAX_LINUX_ID = 4_294_967_294
_STAGING_PREFIX = ".underwrite-evidence-"


def _is_staging_name(value):
    return value.startswith(_STAGING_PREFIX)


def _staging_name(destination_name):
    digest = hashlib.sha256(destination_name.encode("utf-8")).hexdigest()
    return _STAGING_PREFIX + digest


def _absolute_path(value, label):
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value == "/"
        or value.startswith("//")
        or "\x00" in value
        or "\\" in value
        or posixpath.normpath(value) != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise AdapterError(f"{label} must be a normalized absolute POSIX path")
    try:
        encoded = value.encode("utf-8")
        components = [part.encode("utf-8") for part in value[1:].split("/")]
    except UnicodeEncodeError as error:
        raise AdapterError(f"{label} must be valid UTF-8") from error
    if len(encoded) > _MAX_PATH_BYTES or any(
        len(component) > _MAX_PATH_COMPONENT_BYTES for component in components
    ):
        raise AdapterError(f"{label} exceeds the host path limit")
    return value


def _directory_flags():
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise AdapterError("adapter host requires descriptor-relative no-follow opens")
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


def _read_flags():
    if not hasattr(os, "O_NOFOLLOW"):
        raise AdapterError("adapter host requires no-follow file opens")
    return (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _require_protected_directory(details, label):
    mode = stat.S_IMODE(details.st_mode)
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid not in (0, os.geteuid())
        or (mode & 0o022 and not mode & stat.S_ISVTX)
    ):
        raise AdapterError(f"{label} has unprotected directory ancestry")


def _require_consumer_traversal(details, label, consumer_gid):
    mode = stat.S_IMODE(details.st_mode)
    if not (mode & 0o001 or details.st_gid == consumer_gid and mode & 0o010):
        raise AdapterError(f"{label} is not traversable by the consumer group")


def _open_directory(path, label, *, private=False, consumer_gid=None):
    path = _absolute_path(path, label)
    try:
        descriptor = os.open("/", _directory_flags())
    except OSError as error:
        raise AdapterError(f"{label} cannot be opened safely") from error
    try:
        details = os.fstat(descriptor)
        _require_protected_directory(details, label)
        if consumer_gid is not None:
            _require_consumer_traversal(details, label, consumer_gid)
        for component in path[1:].split("/"):
            try:
                child = os.open(component, _directory_flags(), dir_fd=descriptor)
            except OSError as error:
                raise AdapterError(f"{label} cannot be opened safely") from error
            os.close(descriptor)
            descriptor = child
            details = os.fstat(descriptor)
            _require_protected_directory(details, label)
            if consumer_gid is not None:
                _require_consumer_traversal(details, label, consumer_gid)
        details = os.fstat(descriptor)
        if private and (
            details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) & 0o077
        ):
            raise AdapterError(f"{label} must be private and owned by the gateway account")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_parent(path, label, *, private=False):
    path = _absolute_path(path, label)
    parent, name = posixpath.split(path)
    if not name:
        raise AdapterError(f"{label} must name a final path component")
    return _open_directory(parent, f"{label} parent", private=private), name


def _consumer_gid(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _MAX_LINUX_ID
        or value not in {os.getegid(), *os.getgroups()}
    ):
        raise AdapterError("consumerGid must be a supplementary gateway group")
    return value


def _open_evidence_parent(path, consumer_gid):
    path = _absolute_path(path, "evidence directory")
    parent_path, name = posixpath.split(path)
    if not name:
        raise AdapterError("evidence directory must name a final path component")
    parent = _open_directory(
        parent_path,
        "evidence directory parent",
        consumer_gid=consumer_gid,
    )
    details = os.fstat(parent)
    if (
        details.st_uid != os.geteuid()
        or details.st_gid != consumer_gid
        or stat.S_IMODE(details.st_mode) != 0o710
    ):
        os.close(parent)
        raise AdapterError(
            "evidence directory parent must be gateway-owned consumer-group 0710"
        )
    return parent, name


def _directory_contains(ancestor, descendant):
    expected = os.fstat(ancestor)
    current = os.dup(descendant)
    try:
        for _depth in range(_MAX_PATH_BYTES + 1):
            details = os.fstat(current)
            if (details.st_dev, details.st_ino) == (
                expected.st_dev,
                expected.st_ino,
            ):
                return True
            parent = os.open("..", _directory_flags(), dir_fd=current)
            parent_details = os.fstat(parent)
            if (parent_details.st_dev, parent_details.st_ino) == (
                details.st_dev,
                details.st_ino,
            ):
                os.close(parent)
                return False
            os.close(current)
            current = parent
    finally:
        os.close(current)
    raise AdapterError("directory ancestry exceeds the host path limit")


def _identity(details):
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_nlink,
        details.st_uid,
        details.st_gid,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def _read_regular_file(path, label, maximum, *, private=False):
    path = _absolute_path(path, label)
    parent, name = _open_parent(path, label)
    try:
        descriptor = os.open(name, _read_flags(), dir_fd=parent)
    except OSError as error:
        os.close(parent)
        raise AdapterError(f"{label} cannot be opened") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise AdapterError(f"{label} must be a single-link regular file")
        if private and (
            before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) & 0o077
        ):
            raise AdapterError(f"{label} must be private and owned by the gateway account")
        if not 0 < before.st_size <= maximum:
            raise AdapterError(f"{label} has an invalid byte size")
        chunks = []
        size = 0
        while True:
            block = os.read(descriptor, min(64 * 1024, maximum - size + 1))
            if not block:
                break
            chunks.append(block)
            size += len(block)
            if size > maximum:
                raise AdapterError(f"{label} exceeds its byte limit")
        after = os.fstat(descriptor)
        if _identity(before) != _identity(after) or size != before.st_size:
            raise AdapterError(f"{label} changed while it was read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)
        os.close(parent)


def _inspect_regular_file(path, label, *, executable=False, private=False):
    path = _absolute_path(path, label)
    parent, name = _open_parent(path, label)
    try:
        descriptor = os.open(name, _read_flags(), dir_fd=parent)
    except OSError as error:
        os.close(parent)
        raise AdapterError(f"{label} cannot be opened") from error
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or (
            not executable and details.st_nlink != 1
        ):
            raise AdapterError(f"{label} must be a trusted regular file")
        mode = stat.S_IMODE(details.st_mode)
        if executable and (
            not mode & 0o111
            or mode & 0o022
            or details.st_uid not in (0, os.geteuid())
        ):
            raise AdapterError(
                f"{label} must be a trusted non-writable executable file"
            )
        if private and (
            details.st_uid != os.geteuid()
            or mode & 0o077
        ):
            raise AdapterError(f"{label} must be private and owned by the gateway account")
    finally:
        os.close(descriptor)
        os.close(parent)
    return path


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise AdapterError(f"duplicate JSON field {key!r}")
        value[key] = item
    return value


def _json_integer(text):
    digits = text[1:] if text.startswith("-") else text
    if len(digits) > len(str(MAX_JSON_INTEGER)):
        raise AdapterError("JSON integer exceeds the exact integer limit")
    value = int(text)
    if not -MAX_JSON_INTEGER <= value <= MAX_JSON_INTEGER:
        raise AdapterError("JSON integer exceeds the exact integer limit")
    return value


def _unsupported_number(_text):
    raise AdapterError("JSON numbers must be exact integers")


def _check_depth(value):
    pending = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        if depth > _MAX_JSON_DEPTH:
            raise AdapterError("JSON value exceeds the nesting limit")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)


def _load_json(data, label):
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_int=_json_integer,
            parse_float=_unsupported_number,
            parse_constant=_unsupported_number,
        )
    except AdapterError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise AdapterError(f"{label} is not valid bounded JSON") from error
    _check_depth(value)
    if not isinstance(value, dict):
        raise AdapterError(f"{label} must be a JSON object")
    return value


def _exact(value, fields, label):
    if not isinstance(value, dict):
        raise AdapterError(f"{label} must be an object")
    missing = sorted(fields - set(value))
    unknown = sorted(set(value) - fields)
    if missing:
        raise AdapterError(f"{label} is missing {', '.join(missing)}")
    if unknown:
        raise AdapterError(f"{label} has unknown field {', '.join(unknown)}")
    return value


def load_configuration(path):
    """Load one exact private host configuration."""
    value = _load_json(
        _read_regular_file(
            path,
            "gateway configuration",
            _MAX_CONFIG_BYTES,
            private=True,
        ),
        "gateway configuration",
    )
    _exact(value, _CONFIG_FIELDS, "gateway configuration")
    if type(value["version"]) is not int or value["version"] != 1:
        raise AdapterError("gateway configuration version must be 1")
    value["consumerGid"] = _consumer_gid(value["consumerGid"])
    try:
        value["profile"] = validate_execution_profile(value["profile"])
    except ReceiptError as error:
        raise AdapterError("gateway configuration profile is invalid") from error
    if len(
        json.dumps(
            value["profile"],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ) > _MAX_PROFILE_BYTES:
        raise AdapterError("gateway configuration profile exceeds its byte limit")
    for field, label in (
        ("privateKey", "private key"),
        ("publicKey", "public key"),
        ("storeRoot", "gateway store root"),
    ):
        value[field] = _absolute_path(value[field], label)
    _inspect_regular_file(value["privateKey"], "private key", private=True)
    _inspect_regular_file(value["publicKey"], "public key")
    for field, label in (
        ("docker", "Docker executable"),
        ("openssl", "OpenSSL executable"),
        ("git", "Git executable"),
    ):
        value[field] = _inspect_regular_file(
            value[field], label, executable=True
        )
    return value


def _policy(configuration):
    profile = configuration["profile"]
    return GatewayPolicy(
        signer_id=profile["signerId"],
        deployment_id=configuration["deploymentId"],
        runtime_domain_id=configuration["runtimeDomainId"],
        docker_host=configuration["dockerHost"],
        image=configuration["image"],
        platform=configuration["platform"],
        runner_sha256=configuration["runnerSha256"],
        executable=profile["job"]["executable"],
        environment=profile["job"]["environment"],
        sandbox=profile["sandbox"],
        target_uid=configuration["targetUid"],
        target_gid=configuration["targetGid"],
        capability_seconds=configuration["capabilitySeconds"],
        max_source_bundle_bytes=configuration["maxSourceBundleBytes"],
    )


def _prepare_temporary_root(store_root):
    descriptor = _open_directory(
        store_root, "gateway store root", private=True
    )
    try:
        created = False
        try:
            os.mkdir("temporary", 0o700, dir_fd=descriptor)
            created = True
        except FileExistsError:
            pass
        temporary = os.open("temporary", _directory_flags(), dir_fd=descriptor)
        try:
            if created:
                os.fchmod(temporary, 0o700)
            details = os.fstat(temporary)
            if (
                details.st_uid != os.geteuid()
                or stat.S_IMODE(details.st_mode) & 0o077
            ):
                raise AdapterError(
                    "gateway temporary root must be private and owned by the gateway account"
                )
        finally:
            os.close(temporary)
    finally:
        os.close(descriptor)
    return posixpath.join(store_root, "temporary")


@contextmanager
def _trusted_temporary_root(path):
    previous = tempfile.tempdir
    tempfile.tempdir = path
    try:
        yield
    finally:
        tempfile.tempdir = previous


@contextmanager
def _private_creation_mask():
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def _preflight_destination(path, store_root, consumer_gid=None):
    path = _absolute_path(path, "evidence directory")
    consumer_gid = _consumer_gid(
        os.getegid() if consumer_gid is None else consumer_gid
    )
    store = _open_directory(store_root, "gateway store root", private=True)
    try:
        parent, name = _open_evidence_parent(path, consumer_gid)
        try:
            if _is_staging_name(name):
                raise AdapterError("evidence directory uses the reserved staging namespace")
            if _directory_contains(store, parent):
                raise AdapterError(
                    "evidence directory and gateway store must be separate"
                )
            try:
                details = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return path
            if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
                raise AdapterError(
                    "existing evidence destination must be a real directory"
                )
            destination = os.open(name, _directory_flags(), dir_fd=parent)
            try:
                opened = os.fstat(destination)
                if (
                    opened.st_dev != details.st_dev
                    or opened.st_ino != details.st_ino
                    or _directory_contains(destination, store)
                ):
                    raise AdapterError(
                        "evidence directory and gateway store must be separate"
                    )
            finally:
                os.close(destination)
        finally:
            os.close(parent)
    finally:
        os.close(store)
    return path


def _evidence_values(stored, content):
    if not isinstance(stored, StoredExecution):
        raise AdapterError("broker returned an invalid stored execution")
    names = [name for name, _reference in stored.artifacts]
    if len(names) != len(_STORED_ARTIFACTS) or set(names) != _STORED_ARTIFACTS:
        raise AdapterError("stored execution has an unexpected artifact manifest")
    values = {
        "request.json": stored.request,
        "capability.dsse.json": stored.capability,
        "receipt.dsse.json": stored.receipt,
        "output.bundle": content.read(stored.artifact("outputBundle")),
        "stdout": content.read(stored.artifact("stdout")),
        "stderr": content.read(stored.artifact("stderr")),
    }
    if any(not isinstance(value, bytes) for value in values.values()):
        raise AdapterError("stored execution evidence must be bytes")
    return values


def _read_evidence_file(directory, name, expected, consumer_gid):
    try:
        descriptor = os.open(name, _read_flags(), dir_fd=directory)
    except OSError as error:
        raise AdapterError("existing evidence cannot be opened safely") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or before.st_gid != consumer_gid
            or stat.S_IMODE(before.st_mode) != 0o640
            or before.st_size != len(expected)
        ):
            raise AdapterError("existing evidence is not a private single-link file")
        chunks = []
        remaining = len(expected)
        while remaining:
            block = os.read(descriptor, min(remaining, 64 * 1024))
            if not block:
                raise AdapterError("existing evidence is incomplete")
            chunks.append(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise AdapterError("existing evidence exceeds its expected size")
        after = os.fstat(descriptor)
        if _identity(before) != _identity(after) or b"".join(chunks) != expected:
            raise AdapterError("existing evidence conflicts with the stored execution")
    finally:
        os.close(descriptor)


def _verify_existing(parent, name, values, consumer_gid):
    try:
        details = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise AdapterError("existing evidence destination is not a real directory")
    directory = os.open(name, _directory_flags(), dir_fd=parent)
    try:
        opened = os.fstat(directory)
        if (
            opened.st_dev != details.st_dev
            or opened.st_ino != details.st_ino
            or opened.st_uid != os.geteuid()
            or opened.st_gid != consumer_gid
            or stat.S_IMODE(opened.st_mode) not in (0o700, 0o750)
        ):
            raise AdapterError("existing evidence directory is not private")
        try:
            names = set(os.listdir(directory))
        except OSError as error:
            raise AdapterError("existing evidence directory cannot be enumerated") from error
        if names != set(_EVIDENCE_FILES):
            raise AdapterError("existing evidence directory does not have the exact file set")
        for evidence_name in _EVIDENCE_FILES:
            _read_evidence_file(
                directory,
                evidence_name,
                values[evidence_name],
                consumer_gid,
            )
        if stat.S_IMODE(opened.st_mode) == 0o700:
            os.fchmod(directory, 0o750)
        os.fsync(directory)
    finally:
        os.close(directory)
    return True


def _write_evidence_file(directory, name, data, consumer_gid):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    with _private_creation_mask():
        descriptor = os.open(name, flags, 0o600, dir_fd=directory)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise AdapterError("could not write complete execution evidence")
            view = view[written:]
        os.fchown(descriptor, -1, consumer_gid)
        os.fchmod(descriptor, 0o640)
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or details.st_uid != os.geteuid()
            or details.st_gid != consumer_gid
            or stat.S_IMODE(details.st_mode) != 0o640
            or details.st_size != len(data)
        ):
            raise AdapterError("staged evidence is not a private single-link file")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _new_staging_directory(parent, destination_name):
    name = _staging_name(destination_name)
    try:
        with _private_creation_mask():
            os.mkdir(name, 0o700, dir_fd=parent)
    except FileExistsError as error:
        raise AdapterError("evidence staging directory already exists") from error
    return name


def _remove_staging_directory(parent, name, consumer_gid):
    try:
        directory = os.open(name, _directory_flags(), dir_fd=parent)
    except FileNotFoundError:
        return False
    try:
        details = os.fstat(directory)
        if (
            details.st_uid != os.geteuid()
            or details.st_gid not in (os.getegid(), consumer_gid)
            or stat.S_IMODE(details.st_mode) != 0o700
        ):
            raise AdapterError("orphaned evidence staging directory is not private")
        children = os.listdir(directory)
        if not set(children) <= set(_EVIDENCE_FILES):
            raise AdapterError("orphaned evidence staging directory is not recognized")
        for child in children:
            child_details = os.stat(child, dir_fd=directory, follow_symlinks=False)
            initial = (
                child_details.st_gid == os.getegid()
                and stat.S_IMODE(child_details.st_mode) == 0o600
            )
            handed_off = (
                child_details.st_gid == consumer_gid
                and stat.S_IMODE(child_details.st_mode) == 0o640
            )
            transitioning = (
                child_details.st_gid == consumer_gid
                and stat.S_IMODE(child_details.st_mode) == 0o600
            )
            if (
                not stat.S_ISREG(child_details.st_mode)
                or child_details.st_nlink != 1
                or child_details.st_uid != os.geteuid()
                or not (initial or transitioning or handed_off)
            ):
                raise AdapterError("orphaned evidence staging file is not private")
        for child in children:
            os.unlink(child, dir_fd=directory)
    finally:
        os.close(directory)
    os.rmdir(name, dir_fd=parent)
    return True


def _cleanup_orphaned_staging(parent, destination_name, consumer_gid):
    if _remove_staging_directory(
        parent,
        _staging_name(destination_name),
        consumer_gid,
    ):
        os.fsync(parent)


def publish_evidence(stored, content, destination, consumer_gid=None):
    """Atomically publish one exact StoredExecution for the linked consumer."""
    values = _evidence_values(stored, content)
    destination = _absolute_path(destination, "evidence directory")
    consumer_gid = _consumer_gid(
        os.getegid() if consumer_gid is None else consumer_gid
    )
    parent, name = _open_evidence_parent(destination, consumer_gid)
    staging = None
    locked = False
    try:
        if _is_staging_name(name):
            raise AdapterError("evidence directory uses the reserved staging namespace")
        fcntl.flock(parent, fcntl.LOCK_EX)
        locked = True
        _cleanup_orphaned_staging(parent, name, consumer_gid)
        if _verify_existing(parent, name, values, consumer_gid):
            os.fsync(parent)
            return Path(destination)
        staging = _new_staging_directory(parent, name)
        directory = os.open(staging, _directory_flags(), dir_fd=parent)
        try:
            os.fchmod(directory, 0o700)
            for evidence_name in _EVIDENCE_FILES:
                _write_evidence_file(
                    directory,
                    evidence_name,
                    values[evidence_name],
                    consumer_gid,
                )
            os.fchown(directory, -1, consumer_gid)
            os.fsync(directory)
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise AdapterError("evidence destination appeared during publication")
            os.rename(staging, name, src_dir_fd=parent, dst_dir_fd=parent)
            staging = None
            os.fchmod(directory, 0o750)
            os.fsync(directory)
            os.fsync(parent)
            return Path(destination)
        finally:
            os.close(directory)
    finally:
        if staging is not None:
            try:
                _remove_staging_directory(parent, staging, consumer_gid)
            except (AdapterError, OSError):
                pass
        if locked:
            fcntl.flock(parent, fcntl.LOCK_UN)
        os.close(parent)


def _request(path, policy, profile):
    request = _load_json(
        _read_regular_file(path, "execution request", _MAX_REQUEST_BYTES),
        "execution request",
    )
    ExecutionRequest(request, policy)
    for field in ("job", "sandbox", "exitCode"):
        if request.get(field) != profile[field]:
            raise AdapterError(
                f"execution request {field} does not match the trusted profile"
            )
    return request


def run(config_path, request_path, source_bundle, evidence_dir):
    """Execute one reserved request and return its published evidence path."""
    configuration = load_configuration(config_path)
    store_root = _absolute_path(configuration["storeRoot"], "gateway store root")
    policy = _policy(configuration)
    profile = configuration["profile"]
    if policy.executor_id != profile["executorId"]:
        raise AdapterError("gateway policy does not match the trusted profile")
    request = _request(request_path, policy, profile)
    source_bundle = _inspect_regular_file(source_bundle, "source bundle")
    evidence_dir = _preflight_destination(
        evidence_dir,
        store_root,
        configuration["consumerGid"],
    )
    temporary_root = _prepare_temporary_root(store_root)
    with _trusted_temporary_root(temporary_root):
        signer = OpenSSLSigner(
            configuration["privateKey"],
            configuration["publicKey"],
            profile["signerId"],
            openssl=configuration["openssl"],
        )
        if signer.signer_id != profile["signerId"]:
            raise AdapterError("signer identity does not match the trusted profile")
        if signer.key_id != profile["keyId"]:
            raise AdapterError("signing key does not match the trusted profile")
        runner = None
        try:
            runner = DockerRunner(
                policy.image,
                policy.platform,
                policy.docker_host,
                policy.deployment_id,
                policy.runtime_domain_id,
                docker=configuration["docker"],
            )
            broker = ExecutionBroker(
                policy,
                signer,
                store_root,
                runner,
                dedicated_process=True,
                git=configuration["git"],
            )
            stored = broker.execute(request, source_bundle)
        finally:
            if runner is not None:
                runner.close()
        published = publish_evidence(
            stored,
            broker.content,
            evidence_dir,
            configuration["consumerGid"],
        )
    return {"version": 1, "status": "complete", "evidenceDir": str(published)}


class Usage(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"adapter.py: error: {message}", file=sys.stderr)
        raise SystemExit(1)


def parser():
    command = Usage(prog="adapter.py")
    command.add_argument("--config", required=True)
    command.add_argument("--request", required=True)
    command.add_argument("--source-bundle", required=True)
    command.add_argument("--evidence-dir", required=True)
    return command


def _write_result(value):
    sys.stdout.buffer.write(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        result = run(
            arguments.config,
            arguments.request,
            arguments.source_bundle,
            arguments.evidence_dir,
        )
    except AttemptFailed as error:
        _write_result(
            {
                "version": 1,
                "status": "failed",
                "reason": "gateway attempt is terminal without a receipt",
            }
        )
        print(f"adapter.py: {error}", file=sys.stderr)
        return 2
    except ReplayConflict as error:
        if error.state in ("preparing", "ready", "executing", "failed"):
            _write_result(
                {
                    "version": 1,
                    "status": "failed",
                    "reason": "gateway attempt is terminal without a receipt",
                }
            )
            print(f"adapter.py: {error}", file=sys.stderr)
            return 2
        _write_result(
            {
                "version": 1,
                "status": "retry",
                "reason": "retry the same request and source bundle",
            }
        )
        print(f"adapter.py: {error}", file=sys.stderr)
        return 1
    except (
        AdapterError,
        ArtifactError,
        DockerError,
        GatewayError,
        OSError,
        ReceiptError,
        SigningError,
        sqlite3.Error,
        StoreError,
    ) as error:
        _write_result(
            {
                "version": 1,
                "status": "retry",
                "reason": "retry the same request and source bundle",
            }
        )
        print(f"adapter.py: {error}", file=sys.stderr)
        return 1
    _write_result(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
