#!/usr/bin/env python3
"""Apply fixed host resource bounds before replacing this process with Git."""

import os
import resource
import sys


_CPU_SECONDS = 60
_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
_FILE_BYTES = 1024 * 1024 * 1024


def _limit(kind, maximum):
    _soft, hard = resource.getrlimit(kind)
    value = maximum if hard == resource.RLIM_INFINITY else min(maximum, hard)
    resource.setrlimit(kind, (value, value))


def _darwin_address_space_limit(additional_bytes=_MEMORY_BYTES):
    import ctypes

    class TimeValue(ctypes.Structure):
        _fields_ = [("seconds", ctypes.c_int), ("microseconds", ctypes.c_int)]

    class TaskInfo(ctypes.Structure):
        _fields_ = [
            ("virtual_size", ctypes.c_uint64),
            ("resident_size", ctypes.c_uint64),
            ("resident_size_max", ctypes.c_uint64),
            ("user_time", TimeValue),
            ("system_time", TimeValue),
            ("policy", ctypes.c_int),
            ("suspend_count", ctypes.c_int),
        ]

    system = ctypes.CDLL(None)
    system.mach_task_self.restype = ctypes.c_uint
    information = TaskInfo()
    count = ctypes.c_uint(ctypes.sizeof(TaskInfo) // ctypes.sizeof(ctypes.c_uint))
    result = system.task_info(
        system.mach_task_self(),
        20,
        ctypes.cast(ctypes.byref(information), ctypes.POINTER(ctypes.c_int)),
        ctypes.byref(count),
    )
    if result != 0 or count.value != 12:
        raise RuntimeError("could not measure the Git limiter address space")
    return information.virtual_size + additional_bytes


def main():
    if len(sys.argv) < 2 or not os.path.isabs(sys.argv[1]):
        return 64
    _limit(resource.RLIMIT_CORE, 0)
    _limit(resource.RLIMIT_CPU, _CPU_SECONDS)
    memory_limit = (
        _darwin_address_space_limit()
        if sys.platform == "darwin"
        else _MEMORY_BYTES
    )
    _limit(resource.RLIMIT_AS, memory_limit)
    _limit(resource.RLIMIT_FSIZE, _FILE_BYTES)
    _limit(resource.RLIMIT_NOFILE, 256)
    executable = sys.argv[1]
    os.execve(executable, [executable, *sys.argv[2:]], os.environ)
    return 70


if __name__ == "__main__":
    raise SystemExit(main())
