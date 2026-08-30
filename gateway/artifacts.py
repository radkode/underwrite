"""Fail-closed workspace and Git bundle handling for the execution gateway.

This module never checks out untrusted Git content. Bundle objects are imported into a
fresh bare quarantine, decoded as data, and materialized through descriptor-relative
filesystem operations before the synthetic SHA-256 tree is measured.
"""

import hashlib
import os
import re
import signal
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from dataclasses import dataclass
from pathlib import Path


OUTPUT_REF = "refs/underwrite/output"
SOURCE_REFS = ("refs/underwrite/base", "refs/underwrite/head")

_ARCHIVE_MAGIC = b"UNDERWRITE-WS\x00\x01"
_ARCHIVE_COUNT = struct.Struct(">I")
_ARCHIVE_RECORD = struct.Struct(">cIQ")
_MAX_ENTRIES = 20_000
_MAX_PATH_BYTES = 4096
_MAX_PATH_DEPTH = 256
_MAX_TOTAL_PATH_BYTES = 8 * 1024 * 1024
_MAX_BUNDLE_HEADER_BYTES = 64 * 1024
_MAX_GIT_BUNDLE_BYTES = 128 * 1024 * 1024
_MAX_GIT_OUTPUT_BYTES = 80 * 1024 * 1024
_MAX_GIT_DIAGNOSTIC_BYTES = 1024 * 1024
_MAX_GIT_QUARANTINE_BYTES = 384 * 1024 * 1024
_GIT_WALL_SECONDS = 120
_GIT_LIMITER = Path(__file__).with_name("git_limiter.py").resolve()
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ArtifactError(ValueError):
    """An artifact is malformed, unsafe, incomplete, or contextually mismatched."""


@dataclass(frozen=True)
class _TreeMeasurement:
    """Internal accounting for a bounded synthetic tree walk."""

    git_tree: str
    entries: int
    content_bytes: int

@dataclass(frozen=True)
class ArtifactDescriptor:
    """The independently derived descriptor for bundle transport bytes."""

    git_tree: str
    sha256: str
    size: int

    @property
    def gitTree(self):
        return self.git_tree

    @property
    def bytes(self):
        return self.size

    def protocol_value(self):
        return {
            "gitTree": self.git_tree,
            "sha256": self.sha256,
            "bytes": self.size,
        }


@dataclass(frozen=True)
class VerifiedSource:
    """A verified frozen source bundle and its materialized workspace."""

    descriptor: ArtifactDescriptor
    workspace: Path

    @property
    def input_tree(self):
        return self.descriptor.git_tree

    @property
    def git_tree(self):
        return self.descriptor.git_tree


@dataclass(frozen=True)
class BuiltOutputBundle:
    """Exact output bundle bytes paired with their verified descriptor."""

    bundle: bytes
    descriptor: ArtifactDescriptor


@dataclass(frozen=True)
class _Record:
    kind: bytes
    path: bytes
    body: bytes


@dataclass
class _WalkState:
    maximum_bytes: int
    entries: int = 0
    content_bytes: int = 0
    path_bytes: int = 0

    def add_path(self, size):
        self.entries += 1
        if self.entries > _MAX_ENTRIES:
            raise ArtifactError(f"workspace has more than {_MAX_ENTRIES} entries")
        if size > _MAX_TOTAL_PATH_BYTES - self.path_bytes:
            raise ArtifactError("workspace paths exceed their aggregate byte limit")
        self.path_bytes += size

    def add_content(self, size):
        if size > self.maximum_bytes - self.content_bytes:
            raise ArtifactError("workspace content exceeds its byte limit")
        self.content_bytes += size


def _positive_limit(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ArtifactError(f"{label} must be a positive integer")
    return value


def _path_bytes(path, label):
    try:
        value = os.fsencode(os.fspath(path))
    except (TypeError, UnicodeError, ValueError) as error:
        raise ArtifactError(f"{label} is not a filesystem path") from error
    if not value or b"\0" in value:
        raise ArtifactError(f"{label} must be a non-empty path without NUL")
    return value


def _component(value):
    if not value or value in (b".", b"..") or b"/" in value or b"\0" in value:
        raise ArtifactError("workspace contains an invalid path component")
    if value.lower() == b".git":
        raise ArtifactError("workspace must not contain .git metadata")


def _relative_components(path):
    if not path or path.startswith(b"/") or path.endswith(b"/"):
        raise ArtifactError("archive contains an invalid relative path")
    components = tuple(path.split(b"/"))
    if len(path) > _MAX_PATH_BYTES:
        raise ArtifactError("archive path exceeds its byte limit")
    if len(components) > _MAX_PATH_DEPTH:
        raise ArtifactError("archive path exceeds its depth limit")
    for item in components:
        _component(item)
    return components


def _open_flags(directory=False, nofollow=True):
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    if directory:
        if not hasattr(os, "O_DIRECTORY"):
            raise ArtifactError("host does not support descriptor-relative directories")
        flags |= os.O_DIRECTORY
    if nofollow:
        if not hasattr(os, "O_NOFOLLOW"):
            raise ArtifactError("host does not support no-follow filesystem opens")
        flags |= os.O_NOFOLLOW
    return flags


def _critical_stat(value):
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _same_stat(left, right, label):
    if _critical_stat(left) != _critical_stat(right):
        raise ArtifactError(f"{label} changed while it was being measured")


def _hash_object(kind, body):
    header = kind + b" " + str(len(body)).encode("ascii") + b"\0"
    return hashlib.sha256(header + body).digest()


def _read_all(fd, expected_size, state):
    state.add_content(expected_size)
    chunks = []
    remaining = expected_size
    while remaining:
        block = os.read(fd, min(remaining, 1024 * 1024))
        if not block:
            raise ArtifactError("regular file became shorter while being measured")
        chunks.append(block)
        remaining -= len(block)
    if os.read(fd, 1):
        raise ArtifactError("regular file became longer while being measured")
    return b"".join(chunks)


def _walk_directory(fd, prefix, state, objects, records, root=False, depth=0):
    before_directory = os.fstat(fd)
    if not stat.S_ISDIR(before_directory.st_mode):
        raise ArtifactError("workspace root is not a directory")
    try:
        names = [os.fsencode(name) for name in os.listdir(fd)]
    except (OSError, UnicodeError) as error:
        raise ArtifactError(f"could not enumerate workspace directory: {error}") from error
    if not names:
        location = "workspace root" if root else "workspace directory"
        raise ArtifactError(f"{location} must not be empty")
    if len(set(names)) != len(names):
        raise ArtifactError("workspace exposes duplicate path bytes")

    children = []
    for name in names:
        _component(name)
        path = name if not prefix else prefix + b"/" + name
        if len(path) > _MAX_PATH_BYTES:
            raise ArtifactError("workspace path exceeds its byte limit")
        state.add_path(len(path))
        try:
            first = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except OSError as error:
            raise ArtifactError(f"could not inspect workspace entry: {error}") from error

        if stat.S_ISREG(first.st_mode):
            if first.st_nlink != 1:
                raise ArtifactError("workspace regular files must not be hard-linked")
            try:
                child_fd = os.open(name, _open_flags(), dir_fd=fd)
            except OSError as error:
                raise ArtifactError(f"could not safely open regular file: {error}") from error
            try:
                opened = os.fstat(child_fd)
                _same_stat(first, opened, "regular file")
                body = _read_all(child_fd, first.st_size, state)
                _same_stat(first, os.fstat(child_fd), "regular file")
            finally:
                os.close(child_fd)
            try:
                _same_stat(
                    first,
                    os.stat(name, dir_fd=fd, follow_symlinks=False),
                    "regular file",
                )
            except OSError as error:
                raise ArtifactError(
                    f"regular file disappeared during measurement: {error}"
                ) from error
            mode = b"100755" if first.st_mode & 0o111 else b"100644"
            object_id = _hash_object(b"blob", body)
            if records is not None:
                records.append(_Record(b"x" if mode == b"100755" else b"f", path, body))
            if objects is not None:
                objects[object_id.hex()] = (b"blob", body)
            children.append((name, False, mode, object_id))
        elif stat.S_ISLNK(first.st_mode):
            if first.st_nlink != 1:
                raise ArtifactError("workspace symbolic links must not be hard-linked")
            try:
                body = os.readlink(name, dir_fd=fd)
            except OSError as error:
                raise ArtifactError(f"could not read symbolic link: {error}") from error
            body = os.fsencode(body)
            if b"\0" in body:
                raise ArtifactError("symbolic link target contains NUL")
            if len(body) != first.st_size:
                raise ArtifactError("symbolic link size changed during measurement")
            state.add_content(len(body))
            try:
                _same_stat(
                    first,
                    os.stat(name, dir_fd=fd, follow_symlinks=False),
                    "symbolic link",
                )
            except OSError as error:
                raise ArtifactError(
                    f"symbolic link disappeared during measurement: {error}"
                ) from error
            object_id = _hash_object(b"blob", body)
            if records is not None:
                records.append(_Record(b"l", path, body))
            if objects is not None:
                objects[object_id.hex()] = (b"blob", body)
            children.append((name, False, b"120000", object_id))
        elif stat.S_ISDIR(first.st_mode):
            if depth + 1 >= _MAX_PATH_DEPTH:
                raise ArtifactError("workspace exceeds its path depth limit")
            try:
                child_fd = os.open(name, _open_flags(directory=True), dir_fd=fd)
            except OSError as error:
                raise ArtifactError(f"could not safely open directory: {error}") from error
            try:
                _same_stat(first, os.fstat(child_fd), "directory")
                object_id = _walk_directory(
                    child_fd,
                    path,
                    state,
                    objects,
                    records,
                    depth=depth + 1,
                )
                _same_stat(first, os.fstat(child_fd), "directory")
            finally:
                os.close(child_fd)
            try:
                _same_stat(
                    first,
                    os.stat(name, dir_fd=fd, follow_symlinks=False),
                    "directory",
                )
            except OSError as error:
                raise ArtifactError(
                    f"directory disappeared during measurement: {error}"
                ) from error
            children.append((name, True, b"40000", object_id))
        else:
            raise ArtifactError("workspace contains a special or unsupported file")

    children.sort(key=lambda item: item[0] + (b"/" if item[1] else b"\0"))
    tree_body = b"".join(
        mode + b" " + name + b"\0" + object_id
        for name, _is_directory, mode, object_id in children
    )
    tree_id = _hash_object(b"tree", tree_body)
    if objects is not None:
        objects[tree_id.hex()] = (b"tree", tree_body)
    _same_stat(before_directory, os.fstat(fd), "directory")
    return tree_id


def _measure_workspace(root, maximum_bytes, collect_objects=False, collect_records=False):
    maximum_bytes = _positive_limit(maximum_bytes, "maximum_bytes")
    raw_root = _path_bytes(root, "workspace root")
    try:
        first = os.stat(raw_root, follow_symlinks=False)
        root_fd = os.open(raw_root, _open_flags(directory=True))
    except OSError as error:
        raise ArtifactError(f"could not safely open workspace root: {error}") from error
    objects = {} if collect_objects else None
    records = [] if collect_records else None
    state = _WalkState(maximum_bytes)
    try:
        _same_stat(first, os.fstat(root_fd), "workspace root")
        tree_id = _walk_directory(
            root_fd,
            b"",
            state,
            objects,
            records,
            root=True,
        )
        _same_stat(first, os.fstat(root_fd), "workspace root")
    finally:
        os.close(root_fd)
    try:
        _same_stat(first, os.stat(raw_root, follow_symlinks=False), "workspace root")
    except OSError as error:
        raise ArtifactError(f"workspace root disappeared during measurement: {error}") from error
    measurement = _TreeMeasurement(tree_id.hex(), state.entries, state.content_bytes)
    return measurement, objects, records


def synthetic_git_tree(root, *, maximum_bytes):
    """Return the synthetic SHA-256 Git tree for a quiescent workspace."""
    return _measure_workspace(root, maximum_bytes)[0].git_tree


def _encode_archive(records, maximum_bytes):
    maximum_bytes = _positive_limit(maximum_bytes, "maximum_bytes")
    records = sorted(records, key=lambda record: record.path)
    if len(records) > _MAX_ENTRIES:
        raise ArtifactError("workspace archive has too many records")
    chunks = [_ARCHIVE_MAGIC, _ARCHIVE_COUNT.pack(len(records))]
    used = sum(map(len, chunks))
    path_bytes = 0
    for record in records:
        if len(record.path) > _MAX_TOTAL_PATH_BYTES - path_bytes:
            raise ArtifactError("workspace paths exceed their aggregate byte limit")
        path_bytes += len(record.path)
        header = _ARCHIVE_RECORD.pack(record.kind, len(record.path), len(record.body))
        size = len(header) + len(record.path) + len(record.body)
        if size > maximum_bytes - used:
            raise ArtifactError("workspace archive exceeds its byte limit")
        chunks.extend((header, record.path, record.body))
        used += size
    return b"".join(chunks)


def pack_workspace(root, *, maximum_bytes):
    """Return a bounded canonical archive of raw workspace path and body bytes."""
    _measurement, _objects, records = _measure_workspace(
        root,
        maximum_bytes,
        collect_records=True,
    )
    return _encode_archive(records, maximum_bytes)


def _parse_archive(archive, maximum_bytes):
    maximum_bytes = _positive_limit(maximum_bytes, "maximum_bytes")
    if not isinstance(archive, bytes):
        raise ArtifactError("workspace archive must be bytes")
    if len(archive) > maximum_bytes:
        raise ArtifactError("workspace archive exceeds its byte limit")
    minimum = len(_ARCHIVE_MAGIC) + _ARCHIVE_COUNT.size
    if len(archive) < minimum or not archive.startswith(_ARCHIVE_MAGIC):
        raise ArtifactError("workspace archive has an invalid header")
    offset = len(_ARCHIVE_MAGIC)
    count = _ARCHIVE_COUNT.unpack_from(archive, offset)[0]
    offset += _ARCHIVE_COUNT.size
    if not 0 < count <= _MAX_ENTRIES:
        raise ArtifactError("workspace archive has an invalid record count")
    records = []
    prior = None
    leaf_paths = set()
    path_bytes = 0
    for _index in range(count):
        if len(archive) - offset < _ARCHIVE_RECORD.size:
            raise ArtifactError("workspace archive record is truncated")
        kind, path_size, body_size = _ARCHIVE_RECORD.unpack_from(archive, offset)
        offset += _ARCHIVE_RECORD.size
        if kind not in (b"f", b"x", b"l"):
            raise ArtifactError("workspace archive has an invalid record kind")
        if path_size > _MAX_PATH_BYTES or path_size + body_size > len(archive) - offset:
            raise ArtifactError("workspace archive record is truncated")
        path = archive[offset:offset + path_size]
        offset += path_size
        body = archive[offset:offset + body_size]
        offset += body_size
        components = _relative_components(path)
        if path_size > _MAX_TOTAL_PATH_BYTES - path_bytes:
            raise ArtifactError("workspace paths exceed their aggregate byte limit")
        path_bytes += path_size
        if prior is not None and path <= prior:
            raise ArtifactError("workspace archive paths are not canonical and unique")
        if any(components[:depth] in leaf_paths for depth in range(1, len(components))):
            raise ArtifactError("workspace archive places content below a leaf")
        leaf_paths.add(components)
        prior = path
        if kind == b"l" and b"\0" in body:
            raise ArtifactError("workspace archive symbolic link target contains NUL")
        records.append(_Record(kind, path, body))
    if offset != len(archive):
        raise ArtifactError("workspace archive has trailing bytes")
    return records


def _directory_fd(root_fd, components):
    current = os.dup(root_fd)
    try:
        for component in components:
            try:
                os.mkdir(component, 0o700, dir_fd=current)
            except FileExistsError:
                pass
            child = os.open(component, _open_flags(directory=True), dir_fd=current)
            details = os.fstat(child)
            if not stat.S_ISDIR(details.st_mode):
                os.close(child)
                raise ArtifactError("workspace archive parent is not a directory")
            os.close(current)
            current = child
        return current
    except (OSError, ArtifactError) as error:
        os.close(current)
        if isinstance(error, ArtifactError):
            raise
        raise ArtifactError(f"could not create workspace directory: {error}") from error


def _materialize_records(records, destination, maximum_bytes):
    maximum_bytes = _positive_limit(maximum_bytes, "maximum_bytes")
    destination_bytes = _path_bytes(destination, "workspace destination")
    total = 0
    for record in records:
        if len(record.body) > maximum_bytes - total:
            raise ArtifactError("workspace content exceeds its byte limit")
        total += len(record.body)
    try:
        os.mkdir(destination_bytes, 0o700)
        root_fd = os.open(destination_bytes, _open_flags(directory=True))
    except OSError as error:
        raise ArtifactError(f"workspace destination must not already exist: {error}") from error
    try:
        for record in records:
            components = _relative_components(record.path)
            parent_fd = _directory_fd(root_fd, components[:-1])
            try:
                name = components[-1]
                if record.kind == b"l":
                    os.symlink(record.body, name, dir_fd=parent_fd)
                    link_details = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    if not stat.S_ISLNK(link_details.st_mode) or link_details.st_nlink != 1:
                        raise ArtifactError("workspace symbolic link was not created safely")
                else:
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    flags |= getattr(os, "O_CLOEXEC", 0)
                    flags |= getattr(os, "O_NOFOLLOW", 0)
                    file_fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
                    try:
                        view = memoryview(record.body)
                        while view:
                            written = os.write(file_fd, view)
                            if written <= 0:
                                raise ArtifactError("could not write complete workspace file")
                            view = view[written:]
                        os.fchmod(file_fd, 0o755 if record.kind == b"x" else 0o644)
                        details = os.fstat(file_fd)
                        if (
                            not stat.S_ISREG(details.st_mode)
                            or details.st_nlink != 1
                            or details.st_size != len(record.body)
                        ):
                            raise ArtifactError("workspace regular file was not created safely")
                    finally:
                        os.close(file_fd)
            except OSError as error:
                raise ArtifactError(f"could not materialize workspace entry: {error}") from error
            finally:
                os.close(parent_fd)
    finally:
        os.close(root_fd)
    return Path(destination)


def unpack_workspace(archive, destination, *, maximum_bytes):
    """Materialize a bounded workspace archive into a new directory."""
    records = _parse_archive(archive, maximum_bytes)
    workspace = _materialize_records(records, destination, maximum_bytes)
    return synthetic_git_tree(workspace, maximum_bytes=maximum_bytes)


def _git_path(executable):
    candidate = shutil.which(executable) if not os.path.isabs(executable) else executable
    if not candidate:
        raise ArtifactError(f"could not find Git executable {executable!r}")
    return str(Path(candidate).resolve())


def _git_environment(root, executable):
    home = root / "home"
    config = root / "config"
    temporary = root / "tmp"
    for directory in (home, config, temporary):
        directory.mkdir(mode=0o700)
    return {
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": str(home),
        "LC_ALL": "C",
        "PATH": str(Path(executable).parent),
        "TMPDIR": str(temporary),
        "XDG_CONFIG_HOME": str(config),
    }


def _git_deadline():
    return time.monotonic() + _GIT_WALL_SECONDS


def _run_git(
    executable,
    environment,
    repo,
    arguments,
    *,
    input_bytes=None,
    deadline=None,
):
    deadline = _git_deadline() if deadline is None else deadline
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ArtifactError("Git artifact verification timed out")
    command = [
        executable,
        "--no-replace-objects",
        "-c", "core.hooksPath=/dev/null",
        "-c", "core.fsmonitor=false",
        "-c", "core.pager=cat",
        "-c", "protocol.allow=never",
        "-c", "protocol.file.allow=always",
        "-c", "transfer.unpackLimit=1",
        "-c", "transfer.fsckObjects=true",
    ]
    if repo is not None:
        command.extend(("-C", str(repo)))
    command.extend(arguments)
    limited_command = [
        sys.executable,
        "-I",
        "-S",
        str(_GIT_LIMITER),
        *command,
    ]
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as diagnostics:
        try:
            process = subprocess.Popen(
                limited_command,
                stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                stdout=output,
                stderr=diagnostics,
                env=environment,
                start_new_session=True,
            )
        except OSError as error:
            raise ArtifactError(f"could not run trusted Git: {error}") from error
        try:
            process.communicate(input=input_bytes, timeout=remaining)
        except subprocess.TimeoutExpired as error:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise ArtifactError("Git artifact verification timed out") from error
        output_size = os.fstat(output.fileno()).st_size
        diagnostic_size = os.fstat(diagnostics.fileno()).st_size
        if output_size > _MAX_GIT_OUTPUT_BYTES:
            raise ArtifactError("Git artifact verification output exceeds its byte limit")
        if diagnostic_size > _MAX_GIT_DIAGNOSTIC_BYTES:
            raise ArtifactError(
                "Git artifact verification diagnostics exceed their byte limit"
            )
        output.seek(0)
        diagnostics.seek(0)
        raw_output = output.read()
        raw_diagnostics = diagnostics.read()
    if process.returncode:
        detail = raw_diagnostics.decode("utf-8", "replace").strip()
        raise ArtifactError(f"Git artifact verification failed: {detail or 'nonzero exit'}")
    return raw_output


def _initialize_quarantine(root, executable, object_format, deadline):
    repo = root / "objects.git"
    environment = _git_environment(root, executable)
    _run_git(
        executable,
        environment,
        None,
        [
            "init",
            "--bare",
            "--quiet",
            f"--object-format={object_format}",
            "--template=",
            str(repo),
        ],
        deadline=deadline,
    )
    return repo, environment


def _write_exact(path, data):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(os.fsencode(path), flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise ArtifactError("could not write complete quarantine artifact")
            view = view[written:]
        details = os.fstat(fd)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise ArtifactError("quarantine artifact is not a private regular file")
    finally:
        os.close(fd)


def _bundle_header(bundle, object_format, expected_refs):
    boundary = bundle.find(b"\n\n", 0, _MAX_BUNDLE_HEADER_BYTES)
    if boundary < 0:
        raise ArtifactError("Git bundle has no bounded complete header")
    header = bundle[:boundary].split(b"\n")
    if not header or header[0] not in (b"# v2 git bundle", b"# v3 git bundle"):
        raise ArtifactError("Git bundle has an unsupported signature")
    if b"\r" in bundle[:boundary] or b"\0" in bundle[:boundary]:
        raise ArtifactError("Git bundle header contains invalid bytes")
    version = 2 if header[0].startswith(b"# v2") else 3
    capabilities = []
    refs = {}
    for line in header[1:]:
        if not line:
            raise ArtifactError("Git bundle header contains an empty record")
        if line.startswith(b"-"):
            raise ArtifactError("Git bundle prerequisites are not allowed")
        if line.startswith(b"@"):
            capabilities.append(line)
            continue
        try:
            raw_oid, raw_ref = line.split(b" ", 1)
            oid = raw_oid.decode("ascii")
            ref = raw_ref.decode("ascii")
        except (ValueError, UnicodeError) as error:
            raise ArtifactError("Git bundle contains a malformed ref") from error
        matcher = _SHA1 if object_format == "sha1" else _SHA256
        if not matcher.fullmatch(oid) or not ref.startswith("refs/"):
            raise ArtifactError("Git bundle contains a malformed ref")
        if ref in refs:
            raise ArtifactError("Git bundle contains a duplicate ref")
        refs[ref] = oid
    if version == 2:
        if object_format != "sha1" or capabilities:
            raise ArtifactError("Git bundle object format does not match quarantine")
    else:
        expected_capability = f"@object-format={object_format}".encode("ascii")
        if capabilities != [expected_capability]:
            raise ArtifactError("Git bundle has unsupported capabilities")
    if set(refs) != set(expected_refs):
        raise ArtifactError("Git bundle refs do not match the fixed artifact policy")
    return refs


def _quarantine_size(root, maximum_bytes, deadline):
    maximum_bytes = _positive_limit(maximum_bytes, "Git quarantine byte limit")
    total = 0
    entries = 0
    pending = [Path(root)]
    while pending:
        if time.monotonic() >= deadline:
            raise ArtifactError("Git artifact verification timed out")
        directory = pending.pop()
        try:
            children = list(os.scandir(directory))
        except OSError as error:
            raise ArtifactError("Git quarantine cannot be measured") from error
        for child in children:
            entries += 1
            if entries > _MAX_ENTRIES * 2:
                raise ArtifactError("Git quarantine has too many entries")
            try:
                details = child.stat(follow_symlinks=False)
            except OSError as error:
                raise ArtifactError("Git quarantine cannot be measured") from error
            if stat.S_ISDIR(details.st_mode):
                pending.append(Path(child.path))
            elif stat.S_ISREG(details.st_mode):
                if details.st_size > maximum_bytes - total:
                    raise ArtifactError("Git quarantine exceeds its byte limit")
                total += details.st_size
            else:
                raise ArtifactError("Git quarantine contains a special filesystem entry")
    return total


def _quarantine_bundle(
    bundle,
    object_format,
    expected_refs,
    executable,
    deadline,
):
    if not isinstance(bundle, bytes) or not bundle:
        raise ArtifactError("Git bundle must be non-empty bytes")
    if len(bundle) > _MAX_GIT_BUNDLE_BYTES:
        raise ArtifactError("Git bundle exceeds the fixed host byte limit")
    temporary = tempfile.TemporaryDirectory(prefix="underwrite-quarantine-")
    root = Path(temporary.name)
    try:
        refs = _bundle_header(bundle, object_format, expected_refs)
        repo, environment = _initialize_quarantine(
            root, executable, object_format, deadline
        )
        bundle_path = root / "artifact.bundle"
        _write_exact(bundle_path, bundle)
        _run_git(
            executable,
            environment,
            repo,
            ["bundle", "verify", str(bundle_path)],
            deadline=deadline,
        )
        refspecs = [f"+{ref}:{ref}" for ref in expected_refs]
        _run_git(
            executable,
            environment,
            repo,
            [
                "fetch",
                "--atomic",
                "--no-tags",
                "--no-write-fetch-head",
                "--recurse-submodules=no",
                str(bundle_path),
                *refspecs,
            ],
            deadline=deadline,
        )
        quarantine_limit = min(
            _MAX_GIT_QUARANTINE_BYTES,
            max(32 * 1024 * 1024, len(bundle) * 3 + 16 * 1024 * 1024),
        )
        _quarantine_size(root, quarantine_limit, deadline)
        _reject_quarantine_extensions(repo)
        _run_git(
            executable,
            environment,
            repo,
            ["fsck", "--full", "--strict", "--no-reflogs", "--no-progress"],
            deadline=deadline,
        )
        _quarantine_size(root, quarantine_limit, deadline)
        return temporary, repo, environment, refs
    except Exception:
        temporary.cleanup()
        raise


def _reject_quarantine_extensions(repo):
    forbidden = (
        repo / "objects" / "info" / "alternates",
        repo / "shallow",
    )
    if any(path.exists() for path in forbidden):
        raise ArtifactError("Git quarantine uses external or incomplete objects")
    if list((repo / "objects" / "pack").glob("*.promisor")):
        raise ArtifactError("Git quarantine contains promised objects")
    if (repo / "refs" / "replace").exists():
        raise ArtifactError("Git quarantine contains replacement refs")


def _resolve_commit(executable, environment, repo, ref, deadline):
    raw = _run_git(
        executable,
        environment,
        repo,
        ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        deadline=deadline,
    )
    try:
        return raw.decode("ascii").strip()
    except UnicodeError as error:
        raise ArtifactError("Git returned a non-ASCII commit ID") from error


def _git_tree_records(
    executable,
    environment,
    repo,
    ref,
    maximum_bytes,
    deadline,
):
    maximum_bytes = _positive_limit(maximum_bytes, "maximum_workspace_bytes")
    entries = []
    pending = [(ref, b"", 0)]
    nodes = 0
    path_bytes = 0
    while pending:
        treeish, prefix, depth = pending.pop()
        raw = _run_git(
            executable,
            environment,
            repo,
            ["ls-tree", "-z", treeish, "--"],
            deadline=deadline,
        )
        direct = [record for record in raw.split(b"\0") if record]
        if not direct:
            raise ArtifactError("Git tree contains an empty directory")
        prior_key = None
        seen_names = set()
        directories = []
        for record in direct:
            try:
                header, name = record.split(b"\t", 1)
                mode, kind, raw_oid = header.split(b" ", 2)
                oid = raw_oid.decode("ascii")
            except (ValueError, UnicodeError) as error:
                raise ArtifactError("Git tree contains a malformed entry") from error
            _component(name)
            if name in seen_names:
                raise ArtifactError("Git tree contains duplicate names")
            seen_names.add(name)
            is_directory = (mode, kind) == (b"040000", b"tree")
            sort_key = name + (b"/" if is_directory else b"\0")
            if prior_key is not None and sort_key <= prior_key:
                raise ArtifactError("Git tree entries are not canonically ordered")
            prior_key = sort_key
            path = name if not prefix else prefix + b"/" + name
            components = _relative_components(path)
            nodes += 1
            if nodes > _MAX_ENTRIES:
                raise ArtifactError("Git tree has too many entries")
            if len(path) > _MAX_TOTAL_PATH_BYTES - path_bytes:
                raise ArtifactError("Git tree paths exceed their aggregate byte limit")
            path_bytes += len(path)
            if not (_SHA1.fullmatch(oid) or _SHA256.fullmatch(oid)):
                raise ArtifactError("Git tree contains an invalid object ID")
            if is_directory:
                if depth + 1 >= _MAX_PATH_DEPTH:
                    raise ArtifactError("Git tree exceeds its path depth limit")
                directories.append((oid, path, depth + 1))
                continue
            if mode == b"160000" or kind == b"commit":
                raise ArtifactError("Git submodules are not supported")
            mapping = {
                (b"100644", b"blob"): b"f",
                (b"100755", b"blob"): b"x",
                (b"120000", b"blob"): b"l",
            }
            record_kind = mapping.get((mode, kind))
            if record_kind is None:
                raise ArtifactError("Git tree contains an unsupported entry mode")
            entries.append((path, record_kind, oid))
        pending.extend(reversed(directories))

    unique_oids = list(dict.fromkeys(oid for _path, _kind, oid in entries))
    request = b"".join(oid.encode("ascii") + b"\n" for oid in unique_oids)
    checked = _run_git(
        executable,
        environment,
        repo,
        ["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
        input_bytes=request,
        deadline=deadline,
    ).splitlines()
    if len(checked) != len(unique_oids):
        raise ArtifactError("Git did not describe every requested object")
    sizes = {}
    for expected_oid, line in zip(unique_oids, checked):
        fields = line.split(b" ")
        try:
            oid, object_type, raw_size = fields
            size = int(raw_size)
        except (ValueError, TypeError) as error:
            raise ArtifactError("Git returned a malformed object descriptor") from error
        if oid.decode("ascii", "strict") != expected_oid or object_type != b"blob" or size < 0:
            raise ArtifactError("Git tree does not resolve to complete blobs")
        sizes[expected_oid] = size
    total = 0
    for _path, _kind, oid in entries:
        size = sizes[oid]
        if size > maximum_bytes - total:
            raise ArtifactError("Git tree content exceeds its byte limit")
        total += size

    output = _run_git(
        executable,
        environment,
        repo,
        ["cat-file", "--batch"],
        input_bytes=request,
        deadline=deadline,
    )
    bodies = {}
    offset = 0
    for expected_oid in unique_oids:
        newline = output.find(b"\n", offset, offset + 256)
        if newline < 0:
            raise ArtifactError("Git returned a malformed object body header")
        fields = output[offset:newline].split(b" ")
        try:
            oid, object_type, raw_size = fields
            size = int(raw_size)
        except (ValueError, TypeError) as error:
            raise ArtifactError("Git returned a malformed object body header") from error
        start = newline + 1
        end = start + size
        if (
            oid.decode("ascii", "strict") != expected_oid
            or object_type != b"blob"
            or size != sizes[expected_oid]
            or end >= len(output)
            or output[end:end + 1] != b"\n"
        ):
            raise ArtifactError("Git returned a malformed or incomplete object body")
        bodies[expected_oid] = output[start:end]
        offset = end + 1
    if offset != len(output):
        raise ArtifactError("Git returned unexpected object body bytes")

    records = []
    for path, kind, oid in entries:
        body = bodies[oid]
        if kind == b"l" and b"\0" in body:
            raise ArtifactError("Git symbolic link target contains NUL")
        records.append(_Record(kind, path, body))
    records.sort(key=lambda item: item.path)
    return records


def _target_bundle_fields(target):
    if not isinstance(target, dict):
        raise ArtifactError("frozen target must be an object")
    values = {}
    for name, matcher in (
        ("base_sha", _SHA1),
        ("head_sha", _SHA1),
        ("object_bundle_sha256", _SHA256),
    ):
        value = target.get(name)
        if not isinstance(value, str) or not matcher.fullmatch(value):
            raise ArtifactError(f"frozen target {name} is invalid")
        values[name] = value
    size = target.get("object_bundle_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ArtifactError("frozen target object_bundle_bytes is invalid")
    values["object_bundle_bytes"] = size
    return values


def verify_source_bundle(
    bundle,
    target,
    workspace,
    *,
    maximum_workspace_bytes,
    git="git",
):
    """Verify a frozen PR bundle and manually materialize its exact head tree."""
    maximum_workspace_bytes = _positive_limit(
        maximum_workspace_bytes,
        "maximum_workspace_bytes",
    )
    if not isinstance(bundle, bytes):
        raise ArtifactError("source bundle must be bytes")
    expected = _target_bundle_fields(target)
    digest = hashlib.sha256(bundle).hexdigest()
    if (
        len(bundle) != expected["object_bundle_bytes"]
        or digest != expected["object_bundle_sha256"]
    ):
        raise ArtifactError("source bundle bytes do not match the frozen target")
    executable = _git_path(git)
    deadline = _git_deadline()
    temporary, repo, environment, refs = _quarantine_bundle(
        bundle,
        "sha1",
        SOURCE_REFS,
        executable,
        deadline,
    )
    try:
        if (
            refs[SOURCE_REFS[0]] != expected["base_sha"]
            or refs[SOURCE_REFS[1]] != expected["head_sha"]
            or _resolve_commit(
                executable, environment, repo, SOURCE_REFS[0], deadline
            )
            != expected["base_sha"]
            or _resolve_commit(
                executable, environment, repo, SOURCE_REFS[1], deadline
            )
            != expected["head_sha"]
        ):
            raise ArtifactError("source bundle refs do not match the frozen commits")
        records = _git_tree_records(
            executable,
            environment,
            repo,
            SOURCE_REFS[1],
            maximum_workspace_bytes,
            deadline,
        )
        materialized = _materialize_records(records, workspace, maximum_workspace_bytes)
        tree = synthetic_git_tree(
            materialized,
            maximum_bytes=maximum_workspace_bytes,
        )
    finally:
        temporary.cleanup()
    descriptor = ArtifactDescriptor(tree, digest, len(bundle))
    return VerifiedSource(descriptor, materialized)


def _write_loose_object(repo, kind, body):
    header = kind + b" " + str(len(body)).encode("ascii") + b"\0"
    payload = header + body
    object_id = hashlib.sha256(payload).hexdigest()
    directory = repo / "objects" / object_id[:2]
    directory.mkdir(mode=0o700, exist_ok=True)
    destination = directory / object_id[2:]
    if not destination.exists():
        compressed = zlib.compress(payload)
        temporary = directory / (object_id[2:] + ".tmp")
        _write_exact(temporary, compressed)
        os.replace(str(temporary), str(destination))
        destination.chmod(0o444)
    return object_id


def _commit_body(tree_id):
    return (
        f"tree {tree_id}\n"
        "author Underwrite Gateway <gateway@underwrite.invalid> 0 +0000\n"
        "committer Underwrite Gateway <gateway@underwrite.invalid> 0 +0000\n"
        "\n"
        "Underwrite execution output\n"
    ).encode("ascii")


def build_output_bundle(
    workspace,
    *,
    maximum_workspace_bytes,
    maximum_bundle_bytes,
    git="git",
):
    """Build and fresh-quarantine verify the fixed-ref SHA-256 output bundle."""
    maximum_bundle_bytes = _positive_limit(maximum_bundle_bytes, "maximum_bundle_bytes")
    tree, objects, _records = _measure_workspace(
        workspace,
        maximum_workspace_bytes,
        collect_objects=True,
    )
    executable = _git_path(git)
    deadline = _git_deadline()
    with tempfile.TemporaryDirectory(prefix="underwrite-output-") as name:
        root = Path(name)
        repo, environment = _initialize_quarantine(
            root, executable, "sha256", deadline
        )
        for expected_id, (kind, body) in objects.items():
            if _write_loose_object(repo, kind, body) != expected_id:
                raise ArtifactError("output Git object digest mismatch")
        commit_id = _write_loose_object(repo, b"commit", _commit_body(tree.git_tree))
        _run_git(
            executable,
            environment,
            repo,
            ["update-ref", OUTPUT_REF, commit_id],
            deadline=deadline,
        )
        _run_git(
            executable,
            environment,
            repo,
            ["fsck", "--full", "--strict", "--no-reflogs", "--no-progress"],
            deadline=deadline,
        )
        bundle_path = root / "output.bundle"
        _run_git(
            executable,
            environment,
            repo,
            ["bundle", "create", str(bundle_path), OUTPUT_REF],
            deadline=deadline,
        )
        bundle = bundle_path.read_bytes()
    if len(bundle) > maximum_bundle_bytes:
        raise ArtifactError("output bundle exceeds its byte limit")
    expected = ArtifactDescriptor(tree.git_tree, hashlib.sha256(bundle).hexdigest(), len(bundle))
    verified = verify_output_bundle(
        bundle,
        maximum_workspace_bytes=maximum_workspace_bytes,
        maximum_bundle_bytes=maximum_bundle_bytes,
        expected=expected,
        git=executable,
    )
    return BuiltOutputBundle(bundle, verified)


def verify_output_bundle(
    bundle,
    *,
    maximum_workspace_bytes,
    maximum_bundle_bytes,
    expected=None,
    git="git",
):
    """Derive a fixed-ref output descriptor in a fresh SHA-256 quarantine."""
    maximum_workspace_bytes = _positive_limit(
        maximum_workspace_bytes,
        "maximum_workspace_bytes",
    )
    maximum_bundle_bytes = _positive_limit(maximum_bundle_bytes, "maximum_bundle_bytes")
    if not isinstance(bundle, bytes) or not bundle:
        raise ArtifactError("output bundle must be non-empty bytes")
    if len(bundle) > maximum_bundle_bytes:
        raise ArtifactError("output bundle exceeds its byte limit")
    executable = _git_path(git)
    deadline = _git_deadline()
    temporary, repo, environment, refs = _quarantine_bundle(
        bundle,
        "sha256",
        (OUTPUT_REF,),
        executable,
        deadline,
    )
    try:
        if (
            _resolve_commit(executable, environment, repo, OUTPUT_REF, deadline)
            != refs[OUTPUT_REF]
        ):
            raise ArtifactError("output ref must point directly to one commit")
        records = _git_tree_records(
            executable,
            environment,
            repo,
            OUTPUT_REF,
            maximum_workspace_bytes,
            deadline,
        )
        workspace = Path(temporary.name) / "materialized"
        _materialize_records(records, workspace, maximum_workspace_bytes)
        tree = synthetic_git_tree(workspace, maximum_bytes=maximum_workspace_bytes)
        raw_tree = _run_git(
            executable,
            environment,
            repo,
            ["rev-parse", "--verify", "--end-of-options", f"{OUTPUT_REF}^{{tree}}"],
            deadline=deadline,
        ).decode("ascii", "strict").strip()
        if tree != raw_tree:
            raise ArtifactError("output bundle tree is not the materialized synthetic tree")
    finally:
        temporary.cleanup()
    descriptor = ArtifactDescriptor(
        tree,
        hashlib.sha256(bundle).hexdigest(),
        len(bundle),
    )
    if expected is not None and descriptor != expected:
        raise ArtifactError("output bundle does not match its expected descriptor")
    return descriptor
