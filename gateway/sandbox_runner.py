#!/usr/bin/env python3
"""Trusted PID 1 for one isolated Underwrite job."""

import ctypes
import errno
import hashlib
import json
import os
import platform
import posixpath
import resource
import selectors
import signal
import stat
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


sys.path.insert(0, "/opt/underwrite")
import artifacts  # noqa: E402


_FRAME = struct.Struct(">Q")
_MAX_CONTROL_BYTES = 1_000_000
_MAX_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
_MAX_WORKSPACE_BYTES = 64 * 1024 * 1024
_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
_MAX_WALL_SECONDS = 900
_MAX_CPU_SECONDS = 900
_MAX_EXECUTABLE_PATH_BYTES = 4095
_MAX_PATH_COMPONENT_BYTES = 255
_MAX_EXEC_VECTOR_BYTES = 128 * 1024
_MAX_EXEC_STRING_BYTES = 128 * 1024 - 1
_EXEC_POINTER_BYTES = 8
_SHA256 = frozenset("0123456789abcdef")
_WORKSPACE_MOUNT = "/workspace"
_WORKSPACE_ROOT = "/workspace/source"
_RUNNER_PATH = "/opt/underwrite/sandbox_runner.py"
_SANDBOX_FIELDS = {
    "policy",
    "credentials",
    "network",
    "hostWrites",
    "gitHooks",
    "gitFilters",
    "timeout",
    "limits",
}
_LIMIT_FIELDS = {
    "wallSeconds",
    "cpuSeconds",
    "memoryBytes",
    "processes",
    "workspaceBytes",
    "outputBytes",
}
_SANDBOX_LITERALS = {
    "policy": "https://github.com/radkode/underwrite/sandbox-policy/v1",
    "credentials": "absent",
    "network": "denied",
    "hostWrites": "denied",
    "gitHooks": "disabled",
    "gitFilters": "disabled",
    "timeout": "enforced",
}


class RunnerError(RuntimeError):
    """The runner cannot prove that its sandbox contract held."""


class JobFailure(RunnerError):
    """The child did not produce a receipt-eligible clean exit."""


def _exact(value, fields, label):
    if not isinstance(value, dict) or set(value) != fields:
        raise RunnerError(f"{label} has the wrong fields")
    return value


def _positive(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RunnerError(f"{label} must be a positive integer")
    return value


def _sha256(value, label):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _SHA256 for character in value)
    ):
        raise RunnerError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise RunnerError(f"duplicate runner field {key!r}")
        value[key] = item
    return value


def _read_exact(stream, size):
    output = bytearray()
    while len(output) < size:
        chunk = stream.read(size - len(output))
        if not chunk:
            raise RunnerError("gateway control input ended early")
        output.extend(chunk)
    return bytes(output)


def _read_frame(stream, maximum):
    size = _FRAME.unpack(_read_exact(stream, _FRAME.size))[0]
    if size > maximum:
        raise RunnerError("gateway frame exceeds its byte limit")
    return _read_exact(stream, size)


def _write_frame(stream, data):
    if not isinstance(data, bytes):
        raise RunnerError("runner frame must be bytes")
    stream.write(_FRAME.pack(len(data)))
    stream.write(data)
    stream.flush()


def _json_frame(stream, maximum, label):
    data = _read_frame(stream, maximum)
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_duplicates)
    except RunnerError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RunnerError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise RunnerError(f"{label} must be an object")
    return value


def _json_bytes(value):
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise RunnerError("runner result is not canonical JSON") from error


def _utc_now():
    return datetime.now(timezone.utc)


def _timestamp(value=None):
    value = _utc_now() if value is None else value.astimezone(timezone.utc)
    timespec = "microseconds" if value.microsecond else "seconds"
    return value.isoformat(timespec=timespec).replace("+00:00", "Z")


def _parse_timestamp(value, label):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RunnerError(f"{label} is not a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise RunnerError(f"{label} is not a real timestamp") from error
    if _timestamp(parsed) != value:
        raise RunnerError(f"{label} is not a canonical UTC timestamp")
    return parsed


def _capability_window(command):
    issued_at = _parse_timestamp(command["issuedAt"], "capability issuedAt")
    expires_at = _parse_timestamp(command["expiresAt"], "capability expiresAt")
    if expires_at <= issued_at or (expires_at - issued_at).total_seconds() > 300:
        raise RunnerError("capability lifetime is invalid")
    return issued_at, expires_at


def _require_current_capability(issued_at, expires_at):
    now = _utc_now()
    if now < issued_at or now >= expires_at:
        raise JobFailure("capability is not currently valid")


def _validate_relative_path(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise RunnerError("job cwd is invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise RunnerError("job cwd contains a control character")
    if value == ".":
        return ()
    if (
        value.startswith("/")
        or "\\" in value
        or posixpath.normpath(value) != value
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise RunnerError("job cwd must be a normalized relative POSIX path")
    if len(value.encode("utf-8")) > 4096:
        raise RunnerError("job cwd exceeds its byte limit")
    components = tuple(os.fsencode(part) for part in value.split("/"))
    if any(len(component) > _MAX_PATH_COMPONENT_BYTES for component in components):
        raise RunnerError("job cwd component exceeds its byte limit")
    return components


def _validate_executable(value):
    value = _exact(value, {"path", "sha256", "bytes"}, "job executable")
    path = value["path"]
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path == "/"
        or path.startswith("//")
        or "\x00" in path
        or posixpath.normpath(path) != path
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise RunnerError("job executable path is not normalized and absolute")
    encoded = path.encode("utf-8")
    components = tuple(part.encode("utf-8") for part in path[1:].split("/"))
    if len(encoded) > _MAX_EXECUTABLE_PATH_BYTES:
        raise RunnerError("job executable path exceeds its byte limit")
    if any(len(component) > _MAX_PATH_COMPONENT_BYTES for component in components):
        raise RunnerError("job executable path component exceeds its byte limit")
    _sha256(value["sha256"], "job executable digest")
    _positive(value["bytes"], "job executable bytes")
    return value


def _validate_exec_vector(argv, environment):
    sizes = []
    for argument in argv:
        size = len(argument.encode("utf-8"))
        if size > _MAX_EXEC_STRING_BYTES:
            raise RunnerError("job argv contains an oversized exec string")
        sizes.append(size + 1)
    for name, value in environment.items():
        size = len(name.encode("utf-8")) + 1 + len(value.encode("utf-8"))
        if size > _MAX_EXEC_STRING_BYTES:
            raise RunnerError("job environment contains an oversized exec string")
        sizes.append(size + 1)
    pointer_bytes = (len(argv) + len(environment) + 2) * _EXEC_POINTER_BYTES
    if pointer_bytes + sum(sizes) > _MAX_EXEC_VECTOR_BYTES:
        raise RunnerError("job argv and environment exceed the exec vector byte limit")


def _validate_request(value):
    value = _exact(
        value,
        {
            "version",
            "job",
            "sandbox",
            "inputTree",
            "targetUid",
            "targetGid",
            "runnerSha256",
        },
        "sandbox request",
    )
    if type(value["version"]) is not int or value["version"] != 1:
        raise RunnerError("sandbox request version must be 1")
    _sha256(value["inputTree"], "sandbox input tree")
    _sha256(value["runnerSha256"], "sandbox runner digest")
    for name in ("targetUid", "targetGid"):
        identity = _positive(value[name], name)
        if identity > 4_294_967_294:
            raise RunnerError(f"{name} is outside the Linux identity range")

    sandbox = _exact(value["sandbox"], _SANDBOX_FIELDS, "sandbox policy")
    if any(sandbox.get(name) != expected for name, expected in _SANDBOX_LITERALS.items()):
        raise RunnerError("sandbox policy does not match the v1 isolation profile")
    limits = _exact(sandbox["limits"], _LIMIT_FIELDS, "sandbox limits")
    for name in _LIMIT_FIELDS:
        _positive(limits[name], f"sandbox {name}")
    if limits["processes"] != 1:
        raise RunnerError("sandbox process limit must equal 1")
    if not 64 * 1024 * 1024 <= limits["memoryBytes"] <= _MAX_MEMORY_BYTES:
        raise RunnerError("sandbox memory is outside the fixed v1 range")
    if limits["wallSeconds"] > _MAX_WALL_SECONDS:
        raise RunnerError("sandbox wall time exceeds the fixed v1 bound")
    if (
        limits["cpuSeconds"] > _MAX_CPU_SECONDS
        or limits["cpuSeconds"] > limits["wallSeconds"]
    ):
        raise RunnerError("sandbox CPU time exceeds the fixed v1 bound")
    if limits["workspaceBytes"] > _MAX_WORKSPACE_BYTES:
        raise RunnerError("sandbox workspace exceeds the fixed v1 bound")
    if limits["outputBytes"] > _MAX_OUTPUT_BYTES:
        raise RunnerError("sandbox output exceeds the fixed v1 bound")

    job = _exact(
        value["job"],
        {"argv", "cwd", "environment", "executable", "stdin"},
        "job",
    )
    executable = _validate_executable(job["executable"])
    argv = job["argv"]
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(argument, str) or "\x00" in argument for argument in argv)
        or argv[0] != executable["path"]
    ):
        raise RunnerError("job argv is invalid")
    _validate_relative_path(job["cwd"])
    environment = job["environment"]
    if not isinstance(environment, dict):
        raise RunnerError("job environment must be an object")
    for name, item in environment.items():
        if (
            not isinstance(name, str)
            or not name
            or "=" in name
            or "\x00" in name
            or not isinstance(item, str)
            or "\x00" in item
        ):
            raise RunnerError("job environment contains an invalid entry")
    _validate_exec_vector(argv, environment)
    if job["stdin"] != "closed":
        raise RunnerError("job standard input must be closed")
    return value


def _hash_regular_file(path, expected=None):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if not hasattr(os, "O_NOFOLLOW"):
        raise RunnerError("runner requires no-follow file opens")
    flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RunnerError(f"could not safely open {path}") from error
    digest = hashlib.sha256()
    size = 0
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise RunnerError(f"{path} is not a regular file")
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
            size += len(block)
        after = os.fstat(descriptor)
        identity = lambda item: (
            item.st_dev,
            item.st_ino,
            item.st_mode,
            item.st_size,
            item.st_mtime_ns,
            item.st_ctime_ns,
        )
        if identity(before) != identity(after) or size != before.st_size:
            raise RunnerError(f"{path} changed during verification")
        value = {"path": path, "sha256": digest.hexdigest(), "bytes": size}
        if expected is not None and value != expected:
            raise RunnerError(f"{path} does not match its trusted descriptor")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor, value
    except Exception:
        os.close(descriptor)
        raise


def _open_cwd(root, components):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current = os.open(root, flags)
    try:
        for component in components:
            child = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = child
        if not stat.S_ISDIR(os.fstat(current).st_mode):
            raise RunnerError("job cwd is not a directory")
        return current
    except Exception:
        os.close(current)
        raise


def _chown_workspace(root, uid, gid):
    os.chmod(_WORKSPACE_MOUNT, 0o711, follow_symlinks=False)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    current = os.open(root, flags)
    stack = []
    try:
        os.fchown(current, uid, gid)
        entries = os.listdir(current)
        index = 0
        while True:
            if index == len(entries):
                if not stack:
                    return
                entries, index, expected_parent = stack.pop()
                parent = os.open("..", flags, dir_fd=current)
                parent_details = os.fstat(parent)
                if (
                    parent_details.st_dev,
                    parent_details.st_ino,
                ) != expected_parent:
                    os.close(parent)
                    raise RunnerError("workspace directory ancestry changed")
                os.close(current)
                current = parent
                continue

            name = entries[index]
            index += 1
            details = os.stat(name, dir_fd=current, follow_symlinks=False)
            if stat.S_ISDIR(details.st_mode):
                child = os.open(name, flags, dir_fd=current)
                child_details = os.fstat(child)
                if (
                    child_details.st_dev,
                    child_details.st_ino,
                ) != (details.st_dev, details.st_ino):
                    os.close(child)
                    raise RunnerError("workspace directory changed during ownership setup")
                parent_details = os.fstat(current)
                stack.append(
                    (
                        entries,
                        index,
                        (parent_details.st_dev, parent_details.st_ino),
                    )
                )
                os.close(current)
                current = child
                os.fchown(current, uid, gid)
                entries = os.listdir(current)
                index = 0
                continue
            os.chown(
                name,
                uid,
                gid,
                dir_fd=current,
                follow_symlinks=False,
            )
    finally:
        os.close(current)


class _SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint32),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_ushort),
        ("filter", ctypes.POINTER(_SockFilter)),
    ]


def _install_target_seccomp():
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        audit_arch = 0xC000003E
        denied = {
            41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55,
            56, 57, 58, 288, 299, 307, 425, 426, 427, 435,
        }
    elif machine in ("aarch64", "arm64"):
        audit_arch = 0xC00000B7
        denied = {
            198, 199, 200, 201, 202, 203, 204, 205, 206, 207, 208, 209,
            210, 211, 212, 220, 242, 243, 269, 425, 426, 427, 435,
        }
    else:
        raise RunnerError(f"unsupported seccomp architecture {machine!r}")

    load_word = 0x20
    jump_equal = 0x15
    jump_bits_set = 0x45
    return_value = 0x06
    return_kill_process = 0x80000000
    return_errno = 0x00050000 | errno.EPERM
    return_allow = 0x7FFF0000
    instructions = [
        _SockFilter(load_word, 0, 0, 4),
        _SockFilter(jump_equal, 1, 0, audit_arch),
        _SockFilter(return_value, 0, 0, return_kill_process),
        _SockFilter(load_word, 0, 0, 0),
    ]
    if machine in ("x86_64", "amd64"):
        instructions.extend(
            (
                _SockFilter(jump_bits_set, 0, 1, 0x40000000),
                _SockFilter(return_value, 0, 0, return_errno),
            )
        )
    for syscall_number in sorted(denied):
        instructions.extend(
            (
                _SockFilter(jump_equal, 0, 1, syscall_number),
                _SockFilter(return_value, 0, 0, return_errno),
            )
        )
    instructions.append(_SockFilter(return_value, 0, 0, return_allow))
    array_type = _SockFilter * len(instructions)
    program_array = array_type(*instructions)
    filter_pointer = _SockFprog._fields_[1][1]
    program = _SockFprog(
        len(instructions), ctypes.cast(program_array, filter_pointer)
    )
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        raise RunnerError(f"could not set no-new-privileges: {os.strerror(ctypes.get_errno())}")
    if libc.prctl(22, 2, ctypes.byref(program), 0, 0) != 0:
        raise RunnerError(f"could not install target seccomp: {os.strerror(ctypes.get_errno())}")


def _assert_target_capabilities_cleared():
    names = {b"CapInh", b"CapPrm", b"CapEff", b"CapAmb"}
    values = {}
    try:
        with open("/proc/self/status", "rb") as handle:
            for line in handle:
                name, separator, value = line.partition(b":")
                if separator and name in names:
                    values[name] = int(value.strip(), 16)
    except (OSError, ValueError) as error:
        raise RunnerError("could not verify target capabilities") from error
    if set(values) != names or any(values.values()):
        raise RunnerError("target process retained Linux capabilities")


def _pipe():
    if not hasattr(os, "pipe2"):
        raise RunnerError("runner requires close-on-exec pipes")
    read_descriptor, write_descriptor = os.pipe2(os.O_CLOEXEC)
    os.set_blocking(read_descriptor, False)
    return read_descriptor, write_descriptor


def _child(
    executable_fd,
    cwd_fd,
    stdout_fd,
    stderr_fd,
    status_fd,
    request,
    issued_at,
    expires_at,
):
    try:
        job = request["job"]
        limits = request["sandbox"]["limits"]
        os.fchdir(cwd_fd)
        os.close(cwd_fd)
        try:
            os.close(0)
        except OSError:
            pass
        os.dup2(stdout_fd, 1)
        os.dup2(stderr_fd, 2)
        os.umask(0o077)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CPU, (limits["cpuSeconds"], limits["cpuSeconds"]))
        resource.setrlimit(resource.RLIMIT_AS, (limits["memoryBytes"], limits["memoryBytes"]))
        resource.setrlimit(resource.RLIMIT_FSIZE, (limits["workspaceBytes"], limits["workspaceBytes"]))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
        resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))
        os.setgroups([])
        os.setgid(request["targetGid"])
        os.setuid(request["targetUid"])
        _install_target_seccomp()
        _assert_target_capabilities_cleared()
        keep = {0, 1, 2, executable_fd, status_fd}
        try:
            open_descriptors = {
                int(name) for name in os.listdir("/proc/self/fd")
            }
        except (OSError, ValueError) as error:
            raise RunnerError("could not enumerate inherited descriptors") from error
        for descriptor in open_descriptors - keep:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if os.execve not in os.supports_fd:
            raise RunnerError("Python does not support descriptor-pinned execve")
        _require_current_capability(issued_at, expires_at)
        os.execve(executable_fd, job["argv"], job["environment"])
    except BaseException as error:
        detail = (type(error).__name__ + ": " + str(error)).encode("utf-8", "replace")[:4096]
        try:
            os.write(status_fd, detail)
        except OSError:
            pass
        os._exit(127)


def _read_available(descriptor):
    try:
        return os.read(descriptor, 64 * 1024)
    except BlockingIOError:
        return None


def _terminate_child(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _monitor_child(pid, descriptors, limits):
    stdout_fd, stderr_fd, status_fd = descriptors
    streams = {stdout_fd: bytearray(), stderr_fd: bytearray()}
    selector = selectors.DefaultSelector()
    for descriptor in descriptors:
        selector.register(descriptor, selectors.EVENT_READ)
    deadline = time.monotonic() + limits["wallSeconds"]
    wait_status = None
    resource_usage = None
    launch_detail = bytearray()
    failure = None
    try:
        while selector.get_map() or wait_status is None:
            if failure is None and time.monotonic() >= deadline:
                failure = "wall limit exceeded"
                _terminate_child(pid)
            timeout = max(0, min(0.05, deadline - time.monotonic())) if failure is None else 0.05
            for key, _mask in selector.select(timeout):
                block = _read_available(key.fd)
                if block is None:
                    continue
                if not block:
                    selector.unregister(key.fd)
                    os.close(key.fd)
                    continue
                if key.fd == status_fd:
                    if len(launch_detail) < 4096:
                        launch_detail.extend(block[: 4096 - len(launch_detail)])
                else:
                    streams[key.fd].extend(block)
                    if (
                        failure is None
                        and len(streams[stdout_fd]) + len(streams[stderr_fd])
                        > limits["outputBytes"]
                    ):
                        failure = "output limit exceeded"
                        _terminate_child(pid)
            if wait_status is None:
                waited_pid, candidate_status, candidate_usage = os.wait4(pid, os.WNOHANG)
                if waited_pid == pid:
                    wait_status = candidate_status
                    resource_usage = candidate_usage
        if launch_detail:
            raise JobFailure("executable launch failed")
        if failure is not None:
            raise JobFailure(failure)
        if not os.WIFEXITED(wait_status):
            raise JobFailure("job terminated by signal")
        if resource_usage is None:
            raise JobFailure("job resource usage was not captured")
        if resource_usage.ru_utime + resource_usage.ru_stime > limits["cpuSeconds"] + 0.1:
            raise JobFailure("CPU limit exceeded")
        return os.WEXITSTATUS(wait_status), bytes(streams[stdout_fd]), bytes(streams[stderr_fd])
    finally:
        selector.close()
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _run_job(request, executable_fd, cwd_fd, issued_at, expires_at):
    _require_current_capability(issued_at, expires_at)
    stdout_read, stdout_write = _pipe()
    stderr_read, stderr_write = _pipe()
    status_read, status_write = _pipe()
    started_at = _timestamp()
    try:
        pid = os.fork()
    except OSError as error:
        for descriptor in (
            stdout_read,
            stdout_write,
            stderr_read,
            stderr_write,
            status_read,
            status_write,
        ):
            os.close(descriptor)
        raise JobFailure("could not create the isolated job process") from error
    if pid == 0:
        os.close(stdout_read)
        os.close(stderr_read)
        os.close(status_read)
        _child(
            executable_fd,
            cwd_fd,
            stdout_write,
            stderr_write,
            status_write,
            request,
            issued_at,
            expires_at,
        )
    os.close(stdout_write)
    os.close(stderr_write)
    os.close(status_write)
    try:
        exit_code, stdout, stderr = _monitor_child(
            pid,
            (stdout_read, stderr_read, status_read),
            request["sandbox"]["limits"],
        )
    except Exception:
        _terminate_child(pid)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        raise
    finished_at = _timestamp()
    archive = artifacts.pack_workspace(
        _WORKSPACE_ROOT,
        maximum_bytes=request["sandbox"]["limits"]["workspaceBytes"],
    )
    tree = artifacts.synthetic_git_tree(
        _WORKSPACE_ROOT,
        maximum_bytes=request["sandbox"]["limits"]["workspaceBytes"],
    )
    return {
        "status": "exited",
        "failure": "",
        "exitCode": exit_code,
        "startedAt": started_at,
        "finishedAt": finished_at,
        "outputTree": tree,
    }, stdout, stderr, archive


def _failure_result(reason):
    return {
        "status": "failed",
        "failure": reason,
        "exitCode": None,
        "startedAt": None,
        "finishedAt": None,
        "outputTree": None,
    }


def main():
    input_stream = sys.stdin.buffer
    output_stream = sys.stdout.buffer
    ready = False
    executable_fd = None
    cwd_fd = None
    try:
        request = _validate_request(
            _json_frame(input_stream, _MAX_CONTROL_BYTES, "sandbox request")
        )
        workspace = _read_frame(
            input_stream, request["sandbox"]["limits"]["workspaceBytes"]
        )
        runner_fd, runner = _hash_regular_file(_RUNNER_PATH)
        os.close(runner_fd)
        if runner["sha256"] != request["runnerSha256"]:
            raise RunnerError("sandbox runner does not match its trusted digest")
        tree = artifacts.unpack_workspace(
            workspace,
            _WORKSPACE_ROOT,
            maximum_bytes=request["sandbox"]["limits"]["workspaceBytes"],
        )
        if tree != request["inputTree"]:
            raise RunnerError("sandbox input tree does not match the workspace")
        _chown_workspace(
            _WORKSPACE_ROOT, request["targetUid"], request["targetGid"]
        )
        executable_fd, executable = _hash_regular_file(
            request["job"]["executable"]["path"],
            request["job"]["executable"],
        )
        cwd_fd = _open_cwd(
            _WORKSPACE_ROOT, _validate_relative_path(request["job"]["cwd"])
        )
        _write_frame(
            output_stream,
            _json_bytes(
                {
                    "status": "ready",
                    "inputTree": tree,
                    "executable": executable,
                    "runnerSha256": runner["sha256"],
                }
            ),
        )
        ready = True
        command = _json_frame(input_stream, _MAX_CONTROL_BYTES, "runner command")
        if command == {"command": "cancel"}:
            return 0
        command = _exact(
            command,
            {
                "command",
                "capabilityPayloadSha256",
                "issuedAt",
                "expiresAt",
            },
            "runner command",
        )
        if command["command"] != "start":
            raise RunnerError("runner command is not start")
        _sha256(
            command["capabilityPayloadSha256"], "capability payload digest"
        )
        issued_at, expires_at = _capability_window(command)
        try:
            result, stdout, stderr, archive = _run_job(
                request,
                executable_fd,
                cwd_fd,
                issued_at,
                expires_at,
            )
        except JobFailure as error:
            _write_frame(output_stream, _json_bytes(_failure_result(str(error))))
            return 0
        finally:
            os.close(executable_fd)
            executable_fd = None
            os.close(cwd_fd)
            cwd_fd = None
        _write_frame(output_stream, _json_bytes(result))
        _write_frame(output_stream, stdout)
        _write_frame(output_stream, stderr)
        _write_frame(output_stream, archive)
        return 0
    except Exception as error:
        detail = f"{type(error).__name__}: {error}"
        print(detail[:8192], file=sys.stderr, flush=True)
        if ready:
            try:
                _write_frame(
                    output_stream,
                    _json_bytes(_failure_result("sandbox verification failed")),
                )
                return 0
            except Exception:
                pass
        return 70
    finally:
        for descriptor in (executable_fd, cwd_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
