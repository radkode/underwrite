#!/usr/bin/env python3
"""Restore the trusted Docker client's inherited address-space ceiling."""

import os
import resource
import sys


def main():
    if len(sys.argv) < 2 or not os.path.isabs(sys.argv[1]):
        return 64
    executable = sys.argv[1]
    try:
        _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        resource.setrlimit(resource.RLIMIT_AS, (hard, hard))
        os.execve(executable, [executable, *sys.argv[2:]], os.environ)
    except (OSError, ValueError):
        return 70
    return 70


if __name__ == "__main__":
    raise SystemExit(main())
