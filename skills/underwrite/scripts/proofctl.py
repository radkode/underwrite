#!/usr/bin/env python3
"""Run walk claims on the frozen PR head through the execution gateway."""

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

from attested_proof import consume, fail, prove, proofs, request  # noqa: E402
from attested_implementation import ImplementationError  # noqa: E402
from execution_receipt import ReceiptError  # noqa: E402
from gateway.artifacts import ArtifactError  # noqa: E402
from gateway.signing import SigningError  # noqa: E402
from session_store import StoreError  # noqa: E402


class Usage(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"proofctl: error: {message}", file=sys.stderr)
        raise SystemExit(1)


def _command(parser):
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("arguments", nargs="+")


def parser():
    command = Usage(prog="proofctl.py")
    commands = command.add_subparsers(dest="command", required=True)

    run = commands.add_parser("prove")
    run.add_argument("session")
    run.add_argument("beat", type=int)
    run.add_argument("config")
    _command(run)

    reserve = commands.add_parser("request")
    reserve.add_argument("session")
    reserve.add_argument("beat", type=int)
    reserve.add_argument("profile")
    _command(reserve)

    verify = commands.add_parser("consume")
    verify.add_argument("session")
    verify.add_argument("proof", type=int)
    verify.add_argument("profile")
    verify.add_argument("public_key")
    verify.add_argument("evidence_dir")

    reject = commands.add_parser("fail")
    reject.add_argument("session")
    reject.add_argument("proof", type=int)
    reject.add_argument("--reason", required=True)

    listing = commands.add_parser("proofs")
    listing.add_argument("session")
    return command


def run(args):
    if args.command == "prove":
        return prove(args.session, args.beat, args.config, args.arguments, args.exit_code)
    if args.command == "request":
        return request(args.session, args.beat, args.profile, args.arguments, args.exit_code)
    if args.command == "consume":
        return consume(
            args.session, args.proof, args.profile, args.public_key, args.evidence_dir
        )
    if args.command == "fail":
        return fail(args.session, args.proof, args.reason)
    if args.command == "proofs":
        return proofs(args.session)
    raise ImplementationError(f"unknown command {args.command!r}")


def main(argv=None):
    try:
        result = run(parser().parse_args(argv))
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
        print(f"proofctl: {error}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(
        json.dumps(
            result, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        + b"\n"
    )
    if isinstance(result, dict) and result.get("status") == "failed":
        return 2
    if isinstance(result, dict) and result.get("status") == "retry":
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
