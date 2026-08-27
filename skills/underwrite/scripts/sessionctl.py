#!/usr/bin/env python3
"""Operate an underwrite session through its transactional store."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from session_store import SessionStore, StoreError  # noqa: E402
from pr_snapshot import (  # noqa: E402
    TargetMoved,
    capture,
    check,
    check_commit,
    check_worktree,
    review_identity,
    review_receipt,
)


class Usage(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"sessionctl: error: {message}", file=sys.stderr)
        raise SystemExit(1)


def read_json(source):
    if source == "-":
        return json.load(sys.stdin)
    with open(source, encoding="utf-8") as handle:
        return json.load(handle)


def object_input(source, name):
    value = read_json(source)
    if not isinstance(value, dict):
        raise StoreError(f"{name} must be a JSON object")
    return value


def application_input(source, reconciliation=False):
    value = object_input(source, "application envelope")
    if "result" not in value:
        raise StoreError("application envelope requires result")
    beats = value.get("beats", [])
    if not isinstance(beats, list):
        raise StoreError("application envelope beats must be an array")
    evidence = value.get("evidence", "")
    if reconciliation and (not isinstance(evidence, str) or not evidence.strip()):
        raise StoreError("reconciliation envelope requires evidence")
    return value["result"], value.get("session"), beats, evidence


def parser():
    ap = Usage(prog="sessionctl.py")
    commands = ap.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init")
    init.add_argument("session")
    init.add_argument("--handled-seq", type=int)

    snapshot_pr = commands.add_parser("snapshot-pr")
    snapshot_pr.add_argument("session")
    snapshot_pr.add_argument("repo")
    snapshot_pr.add_argument("pr", type=int)

    check_pr = commands.add_parser("check-pr")
    check_pr.add_argument("session")
    check_pr.add_argument("--require-open", action="store_true")

    check_tree = commands.add_parser("check-worktree")
    check_tree.add_argument("session")
    check_tree.add_argument("repo_root")

    pin_branch = commands.add_parser("pin-branch")
    pin_branch.add_argument("session")
    pin_branch.add_argument("branch")

    marker = commands.add_parser("review-marker")
    marker.add_argument("session")

    receipt = commands.add_parser("review-receipt")
    receipt.add_argument("session")
    receipt.add_argument("source")
    receipt.add_argument("--actor", required=True)

    for name in ("put-session", "patch-session", "put-beat"):
        command = commands.add_parser(name)
        command.add_argument("session")
        command.add_argument("source", nargs="?", default="-")

    get_session = commands.add_parser("get-session")
    get_session.add_argument("session")

    get_beat = commands.add_parser("get-beat")
    get_beat.add_argument("session")
    get_beat.add_argument("beat", type=int)

    apply = commands.add_parser("apply")
    apply.add_argument("session")
    apply.add_argument("seq", type=int)
    apply.add_argument("source", nargs="?", default="-")

    ack = commands.add_parser("ack")
    ack.add_argument("session")
    ack.add_argument("seq", type=int)

    land = commands.add_parser("land")
    land.add_argument("session")
    land.add_argument("seq", type=int)
    land.add_argument("beat", type=int)
    land.add_argument("artifact")
    land.add_argument("--kind", choices=("commit", "review"), required=True)
    land.add_argument("--branch")
    land.add_argument("--entry")
    land.add_argument("--repo-root")

    fail = commands.add_parser("fail")
    fail.add_argument("session")
    fail.add_argument("seq", type=int)
    fail.add_argument("error")
    fail.add_argument("owed")

    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("session")

    reconcile_action = commands.add_parser("reconcile-action")
    reconcile_action.add_argument("session")
    reconcile_action.add_argument("seq", type=int)
    reconcile_action.add_argument("source", nargs="?", default="-")

    abandon = commands.add_parser("abandon-head")
    abandon.add_argument("session")
    abandon.add_argument("seq", type=int)
    abandon.add_argument("--actor", required=True)
    abandon.add_argument("--reason", required=True)

    export = commands.add_parser("export")
    export.add_argument("session")
    return ap


def run(args):
    if args.command == "init":
        store = SessionStore(args.session, handled_override=args.handled_seq)
        store.export_json()
        return store.reconcile()

    if args.command == "snapshot-pr":
        store = SessionStore(args.session)
        result = capture(store, args.repo, args.pr)
    elif args.command == "check-pr":
        store = SessionStore(args.session)
        return check(store, require_open=args.require_open)
    elif args.command == "check-worktree":
        store = SessionStore(args.session)
        return check_worktree(store, args.repo_root)
    elif args.command == "pin-branch":
        store = SessionStore(args.session)
        result = store.pin_branch(args.branch)
    elif args.command == "review-marker":
        store = SessionStore(args.session)
        return review_identity(store)
    elif args.command == "review-receipt":
        store = SessionStore(args.session)
        return review_receipt(store, args.source, args.actor)
    elif args.command == "put-session":
        document = object_input(args.source, "session")
        store = SessionStore(args.session)
        result = store.put_session(document)
    elif args.command == "patch-session":
        document = object_input(args.source, "session patch")
        store = SessionStore(args.session)
        result = store.patch_session(document)
    elif args.command == "put-beat":
        document = object_input(args.source, "beat")
        store = SessionStore(args.session)
        result = store.put_beat(document)
    elif args.command == "get-session":
        return SessionStore(args.session).snapshot()[0]
    elif args.command == "get-beat":
        beats = SessionStore(args.session).snapshot()[1]
        try:
            return next(beat for beat in beats if beat["n"] == args.beat)
        except StopIteration as error:
            raise StoreError(f"no beat {args.beat}") from error
    elif args.command == "apply":
        application, session, beats, _evidence = application_input(args.source)
        store = SessionStore(args.session)
        result = store.apply(args.seq, application, session=session, beats=beats)
    elif args.command == "ack":
        store = SessionStore(args.session)
        result = store.ack(args.seq)
    elif args.command == "land":
        entry = object_input(args.entry, "land entry") if args.entry else None
        store = SessionStore(args.session)
        if args.kind == "commit" and "target" in store.snapshot()[0]:
            if not args.repo_root:
                raise StoreError("PR commit delivery requires --repo-root")
            check_commit(
                store,
                args.repo_root,
                args.seq,
                args.beat,
                args.artifact,
                args.branch,
            )
        result = store.land(
            args.seq,
            args.beat,
            args.artifact,
            args.kind,
            branch=args.branch,
            land_entry=entry,
        )
    elif args.command == "fail":
        store = SessionStore(args.session)
        result = store.fail(args.seq, args.error, args.owed)
    elif args.command == "reconcile":
        return SessionStore(args.session).reconcile()
    elif args.command == "reconcile-action":
        application, session, beats, evidence = application_input(
            args.source, reconciliation=True
        )
        store = SessionStore(args.session)
        result = store.reconcile_action(
            args.seq,
            application,
            session=session,
            beats=beats,
            evidence=evidence,
        )
    elif args.command == "abandon-head":
        store = SessionStore(args.session)
        result = store.abandon_head(args.seq, args.actor, args.reason)
    elif args.command == "export":
        return SessionStore(args.session).export_json()
    else:
        raise StoreError(f"unknown command {args.command!r}")

    store.export_json()
    return result


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        result = run(args)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except TargetMoved as error:
        print(f"sessionctl: {error}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error, json.JSONDecodeError, StoreError) as error:
        print(f"sessionctl: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
