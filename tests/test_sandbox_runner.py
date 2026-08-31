#!/usr/bin/env python3
"""Contracts for the trusted in-container execution wrapper."""

import copy
import errno
import hashlib
import io
import json
import os
import struct
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gateway import artifacts as artifact_module


with mock.patch.dict(sys.modules, {"artifacts": artifact_module}):
    from gateway import sandbox_runner


INPUT_TREE = "a" * 64
RUNNER_SHA256 = "b" * 64
CAPABILITY_SHA256 = "c" * 64
ISSUED_AT = datetime.now(timezone.utc) - timedelta(seconds=1)
EXPIRES_AT = ISSUED_AT + timedelta(minutes=5)
ISSUED_TEXT = sandbox_runner._timestamp(ISSUED_AT)
EXPIRES_TEXT = sandbox_runner._timestamp(EXPIRES_AT)
EXECUTABLE_SHA256 = "d" * 64
EXECUTABLE = {
    "path": "/usr/local/bin/python3",
    "sha256": EXECUTABLE_SHA256,
    "bytes": 12_345,
}
LIMITS = {
    "wallSeconds": 2,
    "cpuSeconds": 1,
    "memoryBytes": 64 * 1024 * 1024,
    "processes": 1,
    "workspaceBytes": 1024 * 1024,
    "outputBytes": 1024,
}
SANDBOX = {
    "policy": "https://github.com/radkode/underwrite/sandbox-policy/v1",
    "credentials": "absent",
    "network": "denied",
    "hostWrites": "denied",
    "gitHooks": "disabled",
    "gitFilters": "disabled",
    "timeout": "enforced",
    "limits": LIMITS,
}
REQUEST = {
    "version": 1,
    "job": {
        "argv": ["/usr/local/bin/python3", "-c", "print('ok')"],
        "cwd": ".",
        "environment": {"LANG": "C.UTF-8", "TZ": "UTC"},
        "executable": EXECUTABLE,
        "stdin": "closed",
    },
    "sandbox": SANDBOX,
    "inputTree": INPUT_TREE,
    "targetUid": 65532,
    "targetGid": 65532,
    "runnerSha256": RUNNER_SHA256,
}


def request_value():
    return copy.deepcopy(REQUEST)


def frame(data):
    return struct.pack(">Q", len(data)) + data


def json_frame(value):
    return frame(
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )


def read_frames(data):
    stream = io.BytesIO(data)
    values = []
    while stream.tell() < len(data):
        size_data = stream.read(8)
        if len(size_data) != 8:
            raise AssertionError("truncated test frame")
        size = struct.unpack(">Q", size_data)[0]
        body = stream.read(size)
        if len(body) != size:
            raise AssertionError("truncated test frame body")
        values.append(body)
    return values


class FramingTests(unittest.TestCase):
    def test_json_frame_rejects_duplicate_fields(self):
        stream = io.BytesIO(frame(b'{"command":"cancel","command":"start"}'))
        with self.assertRaisesRegex(sandbox_runner.RunnerError, "duplicate runner field"):
            sandbox_runner._json_frame(stream, 1000, "runner command")

    def test_frame_rejects_oversize_before_body_read(self):
        stream = io.BytesIO(struct.pack(">Q", 101))
        with self.assertRaisesRegex(sandbox_runner.RunnerError, "exceeds"):
            sandbox_runner._read_frame(stream, 100)

    def test_frame_rejects_early_eof(self):
        stream = io.BytesIO(struct.pack(">Q", 4) + b"abc")
        with self.assertRaisesRegex(sandbox_runner.RunnerError, "ended early"):
            sandbox_runner._read_frame(stream, 4)


class RequestValidationTests(unittest.TestCase):
    def test_accepts_only_the_exact_v1_request(self):
        request = request_value()
        self.assertIs(sandbox_runner._validate_request(request), request)

        for location in ((), ("job",), ("sandbox",), ("sandbox", "limits")):
            changed = request_value()
            current = changed
            for component in location:
                current = current[component]
            current["unexpected"] = True
            with self.subTest(location=location):
                with self.assertRaisesRegex(sandbox_runner.RunnerError, "wrong fields"):
                    sandbox_runner._validate_request(changed)

    def test_version_must_be_an_integer_not_a_boolean(self):
        request = request_value()
        request["version"] = True
        with self.assertRaisesRegex(sandbox_runner.RunnerError, "version"):
            sandbox_runner._validate_request(request)

    def test_cwd_requires_a_canonical_relative_posix_path(self):
        invalid = (
            "",
            "/source",
            "source/../elsewhere",
            "source//nested",
            "source/./nested",
            "source\\nested",
            "source\nchild",
            "x" * 256,
            "x" * 4097,
        )
        for value in invalid:
            request = request_value()
            request["job"]["cwd"] = value
            with self.subTest(value=value[:40]):
                with self.assertRaises(sandbox_runner.RunnerError):
                    sandbox_runner._validate_request(request)

        self.assertEqual(sandbox_runner._validate_relative_path("."), ())
        self.assertEqual(
            sandbox_runner._validate_relative_path("source/nested"),
            (b"source", b"nested"),
        )

    def test_executable_requires_one_canonical_absolute_path(self):
        invalid = (
            "usr/local/bin/python3",
            "/",
            "//usr/local/bin/python3",
            "/usr/local/../bin/python3",
            "/usr/local/bin/python3/",
            "/usr/local/bin/py\nthon3",
            "/" + "x" * 256,
            "/" + "/".join("x" * 255 for _ in range(16)),
        )
        for value in invalid:
            request = request_value()
            request["job"]["executable"]["path"] = value
            request["job"]["argv"][0] = value
            with self.subTest(value=value):
                with self.assertRaisesRegex(sandbox_runner.RunnerError, "executable path"):
                    sandbox_runner._validate_request(request)

    def test_argv_and_environment_fit_the_fixed_exec_vector_budget(self):
        invalid = []
        argument = request_value()
        argument["job"]["argv"].append("x" * (128 * 1024))
        invalid.append(argument)
        pointers = request_value()
        pointers["job"]["argv"].extend([""] * 15_000)
        invalid.append(pointers)
        environment = request_value()
        environment["job"]["environment"]["OVERSIZED"] = "x" * (128 * 1024)
        invalid.append(environment)

        for request in invalid:
            with self.subTest(arguments=len(request["job"]["argv"])):
                with self.assertRaisesRegex(sandbox_runner.RunnerError, "exec"):
                    sandbox_runner._validate_request(request)

    def test_argv_must_name_the_verified_executable(self):
        for argv in ([], ["/bin/sh"], [EXECUTABLE["path"], "bad\0argument"]):
            request = request_value()
            request["job"]["argv"] = argv
            with self.subTest(argv=argv):
                with self.assertRaisesRegex(sandbox_runner.RunnerError, "argv"):
                    sandbox_runner._validate_request(request)

    def test_environment_is_exact_data_and_stdin_is_closed(self):
        invalid_environments = (
            [],
            {"": "value"},
            {"BAD=NAME": "value"},
            {"BAD\0NAME": "value"},
            {"NAME": "bad\0value"},
            {"NAME": 1},
        )
        for environment in invalid_environments:
            request = request_value()
            request["job"]["environment"] = environment
            with self.subTest(environment=environment):
                with self.assertRaisesRegex(sandbox_runner.RunnerError, "environment"):
                    sandbox_runner._validate_request(request)

        request = request_value()
        request["job"]["stdin"] = "inherit"
        with self.assertRaisesRegex(sandbox_runner.RunnerError, "standard input"):
            sandbox_runner._validate_request(request)

    def test_limits_and_linux_identities_are_strict_positive_integers(self):
        for value in (True, 0, -1, 1.5, "1"):
            request = request_value()
            request["targetUid"] = value
            with self.subTest(kind="uid", value=value):
                with self.assertRaises(sandbox_runner.RunnerError):
                    sandbox_runner._validate_request(request)

            request = request_value()
            request["sandbox"]["limits"]["wallSeconds"] = value
            with self.subTest(kind="limit", value=value):
                with self.assertRaises(sandbox_runner.RunnerError):
                    sandbox_runner._validate_request(request)

        request = request_value()
        request["sandbox"]["limits"]["processes"] = 2
        with self.assertRaisesRegex(sandbox_runner.RunnerError, "must equal 1"):
            sandbox_runner._validate_request(request)

        request = request_value()
        request["targetGid"] = 4_294_967_295
        with self.assertRaisesRegex(sandbox_runner.RunnerError, "identity range"):
            sandbox_runner._validate_request(request)

        fixed_maxima = (
            ("memoryBytes", 2 * 1024 * 1024 * 1024 + 1),
            ("workspaceBytes", 64 * 1024 * 1024 + 1),
            ("outputBytes", 16 * 1024 * 1024 + 1),
            ("wallSeconds", 901),
            ("cpuSeconds", 901),
        )
        for name, value in fixed_maxima:
            request = request_value()
            request["sandbox"]["limits"][name] = value
            with self.subTest(name=name):
                with self.assertRaises(sandbox_runner.RunnerError):
                    sandbox_runner._validate_request(request)

    def test_sandbox_claims_are_fixed_literals(self):
        for name in sandbox_runner._SANDBOX_LITERALS:
            request = request_value()
            request["sandbox"][name] = "unknown"
            with self.subTest(name=name):
                with self.assertRaisesRegex(sandbox_runner.RunnerError, "isolation profile"):
                    sandbox_runner._validate_request(request)


class CapabilityTests(unittest.TestCase):
    def test_capability_window_is_canonical_positive_and_at_most_five_minutes(self):
        valid = {
            "issuedAt": "2026-08-29T12:00:00Z",
            "expiresAt": "2026-08-29T12:05:00Z",
        }
        issued_at, expires_at = sandbox_runner._capability_window(valid)
        self.assertEqual((expires_at - issued_at).total_seconds(), 300)

        invalid = (
            {
                "issuedAt": "2026-08-29T12:00:00Z",
                "expiresAt": "2026-08-29T12:05:01Z",
            },
            {
                "issuedAt": "2026-08-29T12:00:00Z",
                "expiresAt": "2026-08-29T12:00:00Z",
            },
            {
                "issuedAt": "2026-08-29T12:00:00+00:00",
                "expiresAt": "2026-08-29T12:00:01Z",
            },
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(sandbox_runner.RunnerError):
                    sandbox_runner._capability_window(value)

    def test_expired_capability_stops_before_job_resources_are_created(self):
        issued_at = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
        expires_at = issued_at + timedelta(seconds=1)
        with mock.patch.object(
            sandbox_runner,
            "_utc_now",
            return_value=expires_at,
        ), mock.patch.object(sandbox_runner, "_pipe") as pipe:
            with self.assertRaisesRegex(sandbox_runner.JobFailure, "currently valid"):
                sandbox_runner._run_job(
                    request_value(),
                    20,
                    21,
                    issued_at,
                    expires_at,
                )
        pipe.assert_not_called()


class DescriptorTests(unittest.TestCase):
    def test_workspace_mount_parent_stays_root_owned_and_not_target_writable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            nested = root / "nested"
            nested.mkdir(parents=True)
            (root / "file").write_bytes(b"root")
            (nested / "child").write_bytes(b"nested")
            with mock.patch.object(
                sandbox_runner.os, "chmod"
            ) as chmod, mock.patch.object(
                sandbox_runner.os, "fchown"
            ) as fchown, mock.patch.object(
                sandbox_runner.os, "chown"
            ) as chown:
                sandbox_runner._chown_workspace(str(root), 65532, 65532)

        chmod.assert_called_once_with(
            sandbox_runner._WORKSPACE_MOUNT,
            0o711,
            follow_symlinks=False,
        )
        self.assertEqual(fchown.call_count, 2)
        self.assertEqual(
            {call.args[0] for call in chown.call_args_list},
            {"file", "child"},
        )
        self.assertTrue(
            all(call.kwargs["dir_fd"] >= 0 for call in chown.call_args_list)
        )

    def test_workspace_ownership_accepts_the_maximum_relative_path(self):
        component = "d" * 240
        file_name = "f" * 240
        relative = "/".join([component] * 16 + [file_name])
        self.assertEqual(len(relative.encode("utf-8")), 4096)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            root.mkdir()
            flags = os.O_RDONLY | os.O_DIRECTORY
            current = os.open(root, flags)
            try:
                for _index in range(16):
                    os.mkdir(component, dir_fd=current)
                    child = os.open(component, flags, dir_fd=current)
                    os.close(current)
                    current = child
                descriptor = os.open(
                    file_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=current,
                )
                os.close(descriptor)
            finally:
                os.close(current)

            with mock.patch.object(
                sandbox_runner.os, "chmod"
            ), mock.patch.object(
                sandbox_runner.os, "fchown"
            ) as fchown, mock.patch.object(
                sandbox_runner.os, "chown"
            ) as chown:
                sandbox_runner._chown_workspace(str(root), 65532, 65532)

        self.assertEqual(fchown.call_count, 17)
        chown.assert_called_once()
        self.assertEqual(chown.call_args.args[0], file_name)
        self.assertNotIn("/", chown.call_args.args[0])

    def test_verified_file_descriptor_survives_path_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "executable"
            path.write_bytes(b"trusted executable")
            expected = {
                "path": str(path),
                "sha256": hashlib.sha256(b"trusted executable").hexdigest(),
                "bytes": len(b"trusted executable"),
            }
            descriptor, measured = sandbox_runner._hash_regular_file(
                str(path), expected
            )
            self.addCleanup(os.close, descriptor)
            replacement = Path(temporary) / "replacement"
            replacement.write_bytes(b"replacement")
            os.replace(replacement, path)

            self.assertEqual(measured, expected)
            self.assertEqual(os.read(descriptor, 100), b"trusted executable")
            self.assertEqual(path.read_bytes(), b"replacement")

    def test_descriptor_is_closed_when_trusted_measurement_mismatches(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "executable"
            path.write_bytes(b"content")
            opened = []
            real_open = os.open

            def record_open(*arguments, **keywords):
                descriptor = real_open(*arguments, **keywords)
                opened.append(descriptor)
                return descriptor

            expected = {
                "path": str(path),
                "sha256": "0" * 64,
                "bytes": len(b"content"),
            }
            with mock.patch.object(
                sandbox_runner.os, "open", side_effect=record_open
            ):
                with self.assertRaisesRegex(sandbox_runner.RunnerError, "trusted descriptor"):
                    sandbox_runner._hash_regular_file(str(path), expected)
            self.assertEqual(len(opened), 1)
            with self.assertRaises(OSError) as context:
                os.fstat(opened[0])
            self.assertEqual(context.exception.errno, errno.EBADF)

    def test_file_and_cwd_opens_reject_symbolic_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir()
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)
            executable = root / "tool"
            executable.write_bytes(b"tool")
            executable_link = root / "tool-link"
            executable_link.symlink_to(executable)

            with self.assertRaises(sandbox_runner.RunnerError):
                sandbox_runner._hash_regular_file(str(executable_link))
            with self.assertRaises(OSError):
                sandbox_runner._open_cwd(str(root), (b"link",))

    def test_cwd_descriptor_survives_directory_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "cwd"
            original.mkdir()
            original_inode = original.stat().st_ino
            descriptor = sandbox_runner._open_cwd(str(root), (b"cwd",))
            self.addCleanup(os.close, descriptor)
            original.rename(root / "old-cwd")
            original.mkdir()
            self.assertEqual(os.fstat(descriptor).st_ino, original_inode)
            self.assertNotEqual(original.stat().st_ino, original_inode)


class FakeLibc:
    def __init__(self, failures=None):
        self.failures = failures or {}
        self.calls = []
        self.instructions = None

    def prctl(self, operation, *arguments):
        self.calls.append((operation, arguments))
        if operation == 22:
            program = arguments[1]._obj
            self.instructions = [
                (
                    program.filter[index].code,
                    program.filter[index].jt,
                    program.filter[index].jf,
                    program.filter[index].k,
                )
                for index in range(program.length)
            ]
        return self.failures.get(operation, 0)


class SeccompTests(unittest.TestCase):
    def install_for(self, architecture):
        libc = FakeLibc()
        with mock.patch.object(
            sandbox_runner.platform, "machine", return_value=architecture
        ), mock.patch.object(sandbox_runner.ctypes, "CDLL", return_value=libc):
            sandbox_runner._install_target_seccomp()
        return libc

    def evaluate(self, instructions, architecture, syscall_number):
        accumulator = 0
        index = 0
        while index < len(instructions):
            code, jump_true, jump_false, value = instructions[index]
            if code == 0x20:
                accumulator = architecture if value == 4 else syscall_number
                index += 1
            elif code == 0x15:
                index += 1 + (jump_true if accumulator == value else jump_false)
            elif code == 0x35:
                index += 1 + (jump_true if accumulator >= value else jump_false)
            elif code == 0x45:
                index += 1 + (jump_true if accumulator & value else jump_false)
            elif code == 0x06:
                return value
            else:
                self.fail(f"unsupported test BPF instruction {code:#x}")
        self.fail("seccomp test program fell off the end")

    def test_x86_filter_kills_foreign_arch_and_denies_network_processes_and_io_uring(self):
        libc = self.install_for("x86_64")
        instructions = libc.instructions
        self.assertEqual([call[0] for call in libc.calls], [38, 22])
        self.assertEqual(instructions[0], (0x20, 0, 0, 4))
        expected = {
            41,
            42,
            43,
            44,
            45,
            46,
            47,
            48,
            49,
            50,
            51,
            52,
            53,
            54,
            55,
            56,
            57,
            58,
            288,
            299,
            307,
            425,
            426,
            427,
            435,
        }
        for syscall_number in expected:
            with self.subTest(syscall_number=syscall_number):
                self.assertEqual(
                    self.evaluate(instructions, 0xC000003E, syscall_number),
                    0x00050000 | errno.EPERM,
                )
        self.assertEqual(
            self.evaluate(instructions, 0x40000003, 41),
            0x80000000,
        )
        self.assertEqual(
            self.evaluate(instructions, 0xC000003E, 39),
            0x7FFF0000,
        )

    def test_x86_filter_rejects_x32_syscall_number_bypass(self):
        instructions = self.install_for("x86_64").instructions
        result = self.evaluate(instructions, 0xC000003E, 0x40000000 | 41)
        self.assertNotEqual(result, 0x7FFF0000)

    def test_arm64_filter_uses_native_socket_and_clone_numbers(self):
        libc = self.install_for("aarch64")
        instructions = libc.instructions
        for syscall_number in (198, 199, 200, 203, 220, 242, 425, 426, 427, 435):
            with self.subTest(syscall_number=syscall_number):
                self.assertEqual(
                    self.evaluate(instructions, 0xC00000B7, syscall_number),
                    0x00050000 | errno.EPERM,
                )

    def test_unsupported_architecture_and_prctl_failures_fail_closed(self):
        with mock.patch.object(
            sandbox_runner.platform, "machine", return_value="riscv64"
        ):
            with self.assertRaisesRegex(sandbox_runner.RunnerError, "unsupported"):
                sandbox_runner._install_target_seccomp()

        for operation, message in ((38, "no-new-privileges"), (22, "seccomp")):
            libc = FakeLibc({operation: -1})
            with self.subTest(operation=operation), mock.patch.object(
                sandbox_runner.platform, "machine", return_value="x86_64"
            ), mock.patch.object(
                sandbox_runner.ctypes, "CDLL", return_value=libc
            ), mock.patch.object(
                sandbox_runner.ctypes, "get_errno", return_value=errno.EPERM
            ):
                with self.assertRaisesRegex(sandbox_runner.RunnerError, message):
                    sandbox_runner._install_target_seccomp()


class ChildExit(BaseException):
    pass


class ChildContractTests(unittest.TestCase):
    def test_child_uses_only_the_pinned_executable_exact_environment_and_closed_stdin(self):
        request = request_value()
        executable_fd = 11
        cwd_fd = 12
        stdout_fd = 13
        stderr_fd = 14
        status_fd = 15
        execve = mock.Mock(side_effect=RuntimeError("stop before real exec"))

        with mock.patch.object(sandbox_runner.os, "fchdir") as fchdir, mock.patch.object(
            sandbox_runner.os, "close"
        ) as close, mock.patch.object(
            sandbox_runner.os, "dup2"
        ) as dup2, mock.patch.object(
            sandbox_runner.os, "umask"
        ), mock.patch.object(
            sandbox_runner.resource, "setrlimit"
        ), mock.patch.object(
            sandbox_runner.os, "setgroups"
        ) as setgroups, mock.patch.object(
            sandbox_runner.os, "setgid"
        ) as setgid, mock.patch.object(
            sandbox_runner.os, "setuid"
        ) as setuid, mock.patch.object(
            sandbox_runner, "_install_target_seccomp"
        ) as seccomp, mock.patch.object(
            sandbox_runner, "_assert_target_capabilities_cleared"
        ) as capabilities, mock.patch.object(
            sandbox_runner.os, "listdir", return_value=["300"]
        ), mock.patch.object(
            sandbox_runner.os, "execve", execve
        ), mock.patch.object(
            sandbox_runner.os, "supports_fd", {execve}
        ), mock.patch.object(
            sandbox_runner.os, "write"
        ), mock.patch.object(
            sandbox_runner.os, "_exit", side_effect=ChildExit
        ):
            with self.assertRaises(ChildExit):
                sandbox_runner._child(
                    executable_fd,
                    cwd_fd,
                    stdout_fd,
                    stderr_fd,
                    status_fd,
                    request,
                    ISSUED_AT,
                    EXPIRES_AT,
                )

        fchdir.assert_called_once_with(cwd_fd)
        self.assertIn(mock.call(0), close.mock_calls)
        self.assertEqual(dup2.mock_calls, [mock.call(stdout_fd, 1), mock.call(stderr_fd, 2)])
        setgroups.assert_called_once_with([])
        setgid.assert_called_once_with(request["targetGid"])
        setuid.assert_called_once_with(request["targetUid"])
        seccomp.assert_called_once_with()
        capabilities.assert_called_once_with()
        execve.assert_called_once_with(
            executable_fd,
            request["job"]["argv"],
            request["job"]["environment"],
        )
        self.assertNotIn(mock.call(stdout_fd, 0), dup2.mock_calls)
        self.assertIn(mock.call(300), close.mock_calls)

    def test_child_closes_inherited_descriptors_above_the_nofile_limit(self):
        import fcntl

        if not hasattr(fcntl, "F_DUPFD_CLOEXEC"):
            self.skipTest("platform cannot allocate a high close-on-exec descriptor")
        status_read, status_write = os.pipe()
        executable_fd = os.open(os.devnull, os.O_RDONLY)
        cwd_fd = os.open("/", os.O_RDONLY)
        output_fd = os.open(os.devnull, os.O_WRONLY)
        high_fd = fcntl.fcntl(executable_fd, fcntl.F_DUPFD_CLOEXEC, 300)

        def inspect_exec(_executable, _argv, _environment):
            try:
                os.fstat(high_fd)
            except OSError as error:
                state = b"closed" if error.errno == errno.EBADF else b"error"
            else:
                state = b"leaked"
            os.write(status_write, state)
            os._exit(0)

        try:
            with mock.patch.object(
                sandbox_runner.resource, "setrlimit"
            ), mock.patch.object(
                sandbox_runner.os, "setgroups"
            ), mock.patch.object(
                sandbox_runner.os, "setgid"
            ), mock.patch.object(
                sandbox_runner.os, "setuid"
            ), mock.patch.object(
                sandbox_runner, "_install_target_seccomp"
            ), mock.patch.object(
                sandbox_runner, "_assert_target_capabilities_cleared"
            ), mock.patch.object(
                sandbox_runner.os, "listdir", return_value=[str(high_fd)]
            ), mock.patch.object(
                sandbox_runner.os, "execve", inspect_exec
            ), mock.patch.object(
                sandbox_runner.os, "supports_fd", {inspect_exec}
            ):
                pid = os.fork()
                if pid == 0:
                    os.close(status_read)
                    sandbox_runner._child(
                        executable_fd,
                        cwd_fd,
                        output_fd,
                        output_fd,
                        status_write,
                        request_value(),
                        ISSUED_AT,
                        EXPIRES_AT,
                    )
                    os._exit(126)
            os.close(status_write)
            status_write = None
            state = os.read(status_read, 64)
            waited_pid, wait_status = os.waitpid(pid, 0)
            self.assertEqual(waited_pid, pid)
            self.assertTrue(os.WIFEXITED(wait_status))
            self.assertEqual(os.WEXITSTATUS(wait_status), 0)
            self.assertEqual(state, b"closed")
        finally:
            for descriptor in (
                status_read,
                status_write,
                executable_fd,
                cwd_fd,
                output_fd,
                high_fd,
            ):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass


class MonitorTests(unittest.TestCase):
    def spawn(self, stdout=b"", stderr=b"", launch=b"", exit_code=0, linger=False):
        pairs = []
        for _index in range(3):
            read_descriptor, write_descriptor = os.pipe()
            os.set_blocking(read_descriptor, False)
            pairs.append((read_descriptor, write_descriptor))
        pid = os.fork()
        if pid == 0:
            try:
                for read_descriptor, _write_descriptor in pairs:
                    os.close(read_descriptor)
                for data, (_read_descriptor, write_descriptor) in zip(
                    (stdout, stderr, launch), pairs
                ):
                    if data:
                        os.write(write_descriptor, data)
                    os.close(write_descriptor)
                if linger:
                    while True:
                        time.sleep(60)
                os._exit(exit_code)
            except BaseException:
                os._exit(125)
        for _read_descriptor, write_descriptor in pairs:
            os.close(write_descriptor)
        return pid, tuple(read_descriptor for read_descriptor, _write in pairs)

    def limits(self, **updates):
        value = dict(LIMITS)
        value.update(updates)
        return value

    def test_clean_nonzero_exit_returns_exact_streams(self):
        pid, descriptors = self.spawn(stdout=b"out", stderr=b"err", exit_code=7)
        result = sandbox_runner._monitor_child(pid, descriptors, self.limits())
        self.assertEqual(result, (7, b"out", b"err"))

    def test_launch_detail_is_never_receipt_eligible(self):
        pid, descriptors = self.spawn(launch=b"execve failed", exit_code=127)
        with self.assertRaisesRegex(sandbox_runner.JobFailure, "launch failed"):
            sandbox_runner._monitor_child(pid, descriptors, self.limits())

    def test_combined_output_limit_kills_the_job(self):
        pid, descriptors = self.spawn(stdout=b"123456", stderr=b"12345", linger=True)
        with self.assertRaisesRegex(sandbox_runner.JobFailure, "output limit"):
            sandbox_runner._monitor_child(
                pid,
                descriptors,
                self.limits(outputBytes=10),
            )

    def test_wall_limit_kills_the_job(self):
        pid, descriptors = self.spawn(linger=True)
        with self.assertRaisesRegex(sandbox_runner.JobFailure, "wall limit"):
            sandbox_runner._monitor_child(
                pid,
                descriptors,
                self.limits(wallSeconds=0.01),
            )


class RunJobTests(unittest.TestCase):
    def test_fork_failure_closes_every_created_pipe(self):
        descriptors = ((10, 11), (12, 13), (14, 15))
        with mock.patch.object(
            sandbox_runner, "_pipe", side_effect=descriptors
        ), mock.patch.object(
            sandbox_runner.os, "fork", side_effect=OSError("no process")
        ), mock.patch.object(
            sandbox_runner.os, "close"
        ) as close:
            with self.assertRaisesRegex(sandbox_runner.JobFailure, "could not create"):
                sandbox_runner._run_job(
                    request_value(), 20, 21, ISSUED_AT, EXPIRES_AT
                )
        self.assertEqual(
            {call.args[0] for call in close.mock_calls},
            {10, 11, 12, 13, 14, 15},
        )

    def test_result_binds_streams_archive_tree_and_timestamps(self):
        descriptors = ((10, 11), (12, 13), (14, 15))
        with mock.patch.object(
            sandbox_runner, "_pipe", side_effect=descriptors
        ), mock.patch.object(
            sandbox_runner.os, "fork", return_value=321
        ), mock.patch.object(
            sandbox_runner.os, "close"
        ), mock.patch.object(
            sandbox_runner, "_monitor_child", return_value=(9, b"out", b"err")
        ) as monitor, mock.patch.object(
            sandbox_runner, "_timestamp", side_effect=("start", "finish")
        ), mock.patch.object(
            sandbox_runner.artifacts, "pack_workspace", return_value=b"archive"
        ) as pack, mock.patch.object(
            sandbox_runner.artifacts, "synthetic_git_tree", return_value="e" * 64
        ) as tree:
            result, stdout, stderr, archive = sandbox_runner._run_job(
                request_value(), 20, 21, ISSUED_AT, EXPIRES_AT
            )

        monitor.assert_called_once_with(321, (10, 12, 14), LIMITS)
        pack.assert_called_once_with(
            sandbox_runner._WORKSPACE_ROOT,
            maximum_bytes=LIMITS["workspaceBytes"],
        )
        tree.assert_called_once_with(
            sandbox_runner._WORKSPACE_ROOT,
            maximum_bytes=LIMITS["workspaceBytes"],
        )
        self.assertEqual(
            result,
            {
                "status": "exited",
                "failure": "",
                "exitCode": 9,
                "startedAt": "start",
                "finishedAt": "finish",
                "outputTree": "e" * 64,
            },
        )
        self.assertEqual((stdout, stderr, archive), (b"out", b"err", b"archive"))


class MainProtocolTests(unittest.TestCase):
    def run_main(self, command, run_result=None, run_error=None):
        request = request_value()
        workspace = b"workspace archive"
        stdin = SimpleNamespace(
            buffer=io.BytesIO(json_frame(request) + frame(workspace) + json_frame(command))
        )
        stdout = SimpleNamespace(buffer=io.BytesIO())
        stderr = io.StringIO()
        fake_sys = SimpleNamespace(stdin=stdin, stdout=stdout, stderr=stderr)
        runner_measurement = {
            "path": sandbox_runner._RUNNER_PATH,
            "sha256": RUNNER_SHA256,
            "bytes": 123,
        }
        executable = copy.deepcopy(EXECUTABLE)
        if run_result is None:
            run_result = (
                {
                    "status": "exited",
                    "failure": "",
                    "exitCode": 0,
                    "startedAt": "start",
                    "finishedAt": "finish",
                    "outputTree": "f" * 64,
                },
                b"stdout",
                b"stderr",
                b"output archive",
            )

        run_job = mock.Mock(return_value=run_result, side_effect=run_error)
        with mock.patch.object(sandbox_runner, "sys", fake_sys), mock.patch.object(
            sandbox_runner,
            "_hash_regular_file",
            side_effect=((50, runner_measurement), (51, executable)),
        ), mock.patch.object(
            sandbox_runner.os, "close"
        ), mock.patch.object(
            sandbox_runner.artifacts, "unpack_workspace", return_value=INPUT_TREE
        ) as unpack, mock.patch.object(
            sandbox_runner, "_chown_workspace"
        ), mock.patch.object(
            sandbox_runner, "_open_cwd", return_value=52
        ), mock.patch.object(
            sandbox_runner, "_run_job", run_job
        ):
            return_code = sandbox_runner.main()
        unpack.assert_called_once_with(
            workspace,
            sandbox_runner._WORKSPACE_ROOT,
            maximum_bytes=LIMITS["workspaceBytes"],
        )
        return return_code, read_frames(stdout.buffer.getvalue()), stderr.getvalue(), run_job

    def test_cancel_after_verified_readiness_never_launches(self):
        return_code, frames, stderr, run_job = self.run_main({"command": "cancel"})
        self.assertEqual(return_code, 0)
        self.assertEqual(len(frames), 1)
        self.assertEqual(json.loads(frames[0]), {
            "status": "ready",
            "inputTree": INPUT_TREE,
            "executable": EXECUTABLE,
            "runnerSha256": RUNNER_SHA256,
        })
        self.assertEqual(stderr, "")
        run_job.assert_not_called()

    def test_clean_run_emits_result_then_exact_stream_and_archive_frames(self):
        return_code, frames, stderr, run_job = self.run_main(
            {
                "command": "start",
                "capabilityPayloadSha256": CAPABILITY_SHA256,
                "issuedAt": ISSUED_TEXT,
                "expiresAt": EXPIRES_TEXT,
            }
        )
        self.assertEqual(return_code, 0)
        self.assertEqual(len(frames), 5)
        self.assertEqual(json.loads(frames[0])["status"], "ready")
        self.assertEqual(json.loads(frames[1])["status"], "exited")
        self.assertEqual(frames[2:], [b"stdout", b"stderr", b"output archive"])
        self.assertEqual(stderr, "")
        run_job.assert_called_once()

    def test_job_failure_emits_no_partial_execution_artifacts(self):
        return_code, frames, stderr, run_job = self.run_main(
            {
                "command": "start",
                "capabilityPayloadSha256": CAPABILITY_SHA256,
                "issuedAt": ISSUED_TEXT,
                "expiresAt": EXPIRES_TEXT,
            },
            run_error=sandbox_runner.JobFailure("output limit exceeded"),
        )
        self.assertEqual(return_code, 0)
        self.assertEqual(len(frames), 2)
        self.assertEqual(
            json.loads(frames[1]),
            sandbox_runner._failure_result("output limit exceeded"),
        )
        self.assertEqual(stderr, "")
        run_job.assert_called_once()


if __name__ == "__main__":
    unittest.main()
