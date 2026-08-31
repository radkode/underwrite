#!/usr/bin/env python3
"""Operate linked, attested PR implementation sessions."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for path in (HERE, ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from attested_implementation import (  # noqa: E402
    ImplementationError,
    consume,
    fail,
    land,
    link,
    request,
)
from execution_receipt import ReceiptError  # noqa: E402
from gateway.artifacts import ArtifactError  # noqa: E402
from gateway.signing import SigningError  # noqa: E402
from pr_snapshot import TargetMoved  # noqa: E402
from session_store import StoreError  # noqa: E402


class Usage(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"implementationctl: error: {message}", file=sys.stderr)
        raise SystemExit(1)


def parser():
    command = Usage(prog="implementationctl.py")
    commands = command.add_subparsers(dest="command", required=True)

    create = commands.add_parser("link")
    create.add_argument("source")
    create.add_argument("repo_root")
    create.add_argument("--seq", type=int, required=True)
    create.add_argument("--beat", type=int, required=True)
    create.add_argument("--actor", required=True)
    create.add_argument("--approval", required=True)

    reserve = commands.add_parser("request")
    reserve.add_argument("child")
    reserve.add_argument("source")
    reserve.add_argument("profile")

    verify = commands.add_parser("consume")
    verify.add_argument("child")
    verify.add_argument("source")
    verify.add_argument("profile")
    verify.add_argument("public_key")
    verify.add_argument("evidence_dir")

    reject = commands.add_parser("fail")
    reject.add_argument("child")
    reject.add_argument("source")
    reject.add_argument("--attempt", type=int, required=True)
    reject.add_argument("--reason", required=True)

    finish = commands.add_parser("land")
    finish.add_argument("child")
    finish.add_argument("source")
    finish.add_argument("repo_root")
    finish.add_argument("--attempt", type=int, required=True)
    finish.add_argument("--message", required=True)
    return command


def run(args):
    if args.command == "link":
        return link(
            args.source,
            args.repo_root,
            seq=args.seq,
            beat=args.beat,
            actor=args.actor,
            approval=args.approval,
        )
    if args.command == "request":
        return request(args.child, args.source, args.profile)
    if args.command == "consume":
        return consume(
            args.child,
            args.source,
            args.profile,
            args.public_key,
            args.evidence_dir,
        )
    if args.command == "fail":
        return fail(
            args.child,
            args.source,
            attempt=args.attempt,
            reason=args.reason,
        )
    if args.command == "land":
        return land(
            args.child,
            args.source,
            args.repo_root,
            attempt=args.attempt,
            message=args.message,
        )
    raise ImplementationError(f"unknown command {args.command!r}")


def main(argv=None):
    try:
        result = run(parser().parse_args(argv))
        sys.stdout.buffer.write(
            json.dumps(
                result,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        return 0
    except TargetMoved as error:
        print(f"implementationctl: {error}", file=sys.stderr)
        return 2
    except (
        ArtifactError,
        ImplementationError,
        OSError,
        ReceiptError,
        SigningError,
        sqlite3.Error,
        StoreError,
        UnicodeError,
        json.JSONDecodeError,
    ) as error:
        print(f"implementationctl: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
