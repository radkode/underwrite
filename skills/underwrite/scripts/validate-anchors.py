#!/usr/bin/env python3
"""Validate GitHub review comment anchors against a PR diff.

GitHub rejects an entire review if a single comment anchors to a line outside the
diff, so this runs before every post. Anchors that land near a hunk are snapped to
the nearest valid line; anchors with no hunk to attach to are folded into the
review body so the observation survives.

  validate-anchors.py --session review-dir --payload payload.json --out payload.fixed.json
  validate-anchors.py --diff pr.diff --payload payload.json

Exit 0 = every anchor was already valid, 2 = anchors were snapped or folded,
1 = usage or parse error.
"""

import argparse
import json
import re
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from session_store import SessionStore, StoreError  # noqa: E402

REVIEW_MARKER = re.compile(r"<!-- underwrite-review:[0-9a-f]{32} -->")

from diff_lines import (  # noqa: E402,F401
    HUNK, SNAP_WINDOW, clean_path, hunk_holding, mark, parse_diff, snap,
)


def validate(payload, files):
    kept, folded, report = [], [], []

    for c in payload.get("comments", []):
        path = c.get("path", "")
        side = c.get("side", "RIGHT")
        line = c.get("line")
        sides = files.get(path)
        valid = (sides or {}).get(side, set())

        if line in valid:
            fixed = line
        else:
            fixed = snap(line, valid) if isinstance(line, int) else None
            if fixed is None:
                folded.append(c)
                if sides is None:
                    why = "file not in diff"
                elif not valid:
                    why = "file has no %s lines in the diff" % side
                else:
                    why = "no hunk within %d lines" % SNAP_WINDOW
                report.append("fold  %s:%s %s  (%s)" % (path, line, side, why))
                continue
            report.append("snap  %s %s  %s -> %s" % (path, side, line, fixed))
            c["line"] = fixed

        # A multiline comment carries its start too, and GitHub rejects the whole
        # review for a start outside the diff, in a different hunk, or not before the
        # end. This has to run even when the end needed no fixing, which is where a bad
        # start used to hide.
        start = c.get("start_line")
        if start is not None:
            start_side = c.get("start_side", side)
            hunk = hunk_holding(sides or {}, side, fixed)
            why = None
            if hunk is None or (start_side, start) not in hunk["pos"]:
                # Comparing the numbers alone let a range span the gap between two
                # hunks, which is the exact 422 this script exists to prevent.
                why = "not in the same hunk"
            elif hunk["pos"][(start_side, start)] >= hunk["pos"][(side, fixed)]:
                # By position, not by number: an old-file line and a new-file line are
                # different coordinate spaces, and comparing them stripped valid
                # mixed-side ranges while passing genuinely inverted ones.
                why = "start does not come first"
            if why:
                c.pop("start_line", None)
                c.pop("start_side", None)
                report.append(
                    "      dropped start_line %s on %s:%s (%s)" % (start, path, fixed, why)
                )
        kept.append(c)

    payload["comments"] = kept

    if folded:
        lines = ["`%s:%s` %s" % (c.get("path"), c.get("line"), c.get("body", "").strip()) for c in folded]
        payload["body"] = (payload.get("body", "").rstrip() + "\n\n" + "\n".join(lines)).strip()

    return report


class Usage(argparse.ArgumentParser):
    """argparse exits 2 on a usage error, and 2 already means "anchors moved, carry
    on" to Phase 4. Forgetting --diff read as success, and the review that followed
    posted whatever --out happened to hold from the run before."""

    def error(self, message):
        sys.exit(f"validate-anchors: {message}")


def main():
    ap = Usage()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--session", help="session with a frozen PR target")
    src.add_argument("--diff", help="path to a unified diff, or - for stdin")
    ap.add_argument("--payload", required=True, help="review payload JSON")
    ap.add_argument("--out", help="where to write the corrected payload (default stdout)")
    args = ap.parse_args()

    try:
        # All three reads take bytes on purpose. Text mode translates a lone \r inside
        # a line's content to \n, desyncing the hunk before parse_diff ever runs.
        target = None
        if args.session:
            root = Path(args.session).expanduser()
            store = SessionStore(root)
            target, diff_bytes = store.read_verified_target_diff()
            marker = store.review_marker()
            diff = diff_bytes.decode("utf-8")
        elif args.diff == "-":
            diff = sys.stdin.buffer.read().decode("utf-8")
        else:
            with open(args.diff, "rb") as fh:
                diff = fh.read().decode("utf-8")

        with open(args.payload, encoding="utf-8") as fh:
            payload = json.load(fh)
        if not isinstance(payload, dict):
            raise StoreError("review payload must be an object")
        if target is not None:
            if payload.get("event") not in (
                "COMMENT",
                "APPROVE",
                "REQUEST_CHANGES",
            ):
                raise StoreError(
                    "review payload event must submit a final review, not leave it pending"
                )
            commit_id = payload.get("commit_id")
            if commit_id is None:
                payload["commit_id"] = target["head_sha"]
            elif commit_id != target["head_sha"]:
                raise StoreError("review payload commit_id does not match the frozen head")
            body = payload.get("body", "")
            if body is None:
                body = ""
            if not isinstance(body, str):
                raise StoreError("review payload body must be text")
            markers = REVIEW_MARKER.findall(body)
            if markers and markers != [marker]:
                raise StoreError("review payload has a conflicting delivery marker")
            if not markers:
                payload["body"] = (body.rstrip() + "\n\n" + marker).strip()
    except (OSError, UnicodeError, json.JSONDecodeError, StoreError) as e:
        sys.exit("validate-anchors: %s" % e)

    report = validate(payload, parse_diff(diff))
    out = json.dumps(payload, indent=2)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(out + "\n")
    else:
        print(out)

    if report:
        print("\n".join(report), file=sys.stderr)
        sys.exit(2)

    print("all anchors valid", file=sys.stderr)


if __name__ == "__main__":
    main()
