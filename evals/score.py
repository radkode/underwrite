#!/usr/bin/env python3
"""Score one finished walk against an eval case: did it flag what mattered, and nothing else."""

import json
import sqlite3
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "underwrite" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from diff_lines import LOCATION_LINE  # noqa: E402
from session_store import FLAG_STATES, SessionStore  # noqa: E402

KINDS = ("miss", "clean")
WINDOW = 3
SHA = set("0123456789abcdef")


class CaseError(ValueError):
    pass


def load_case(path):
    case = json.loads(Path(path).read_text(encoding="utf-8"))
    problems = case_problems(case)
    if problems:
        raise CaseError(f"{path}: " + "; ".join(problems))
    return case


def case_problems(case):
    problems = []
    for field in ("id", "kind", "repo", "number", "base_sha", "head_sha", "expected", "evidence"):
        if field not in case:
            problems.append(f"missing {field}")
    if problems:
        return problems
    if case["kind"] not in KINDS:
        problems.append(f"kind must be one of {', '.join(KINDS)}")
    for field in ("base_sha", "head_sha"):
        value = case[field]
        if not (isinstance(value, str) and len(value) == 40 and set(value) <= SHA):
            problems.append(f"{field} must be a full lowercase SHA")
    expected = case["expected"]
    if not isinstance(expected, list):
        return problems + ["expected must be a list"]
    if case["kind"] == "miss" and not expected:
        problems.append("a miss case names what the walk should have flagged")
    if case["kind"] == "clean" and expected:
        problems.append("a clean case expects no finding")
    for item in expected:
        if not (
            isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and isinstance(item.get("lines"), list)
            and item["lines"]
            and all(isinstance(n, int) and n > 0 for n in item["lines"])
            and isinstance(item.get("class"), str)
        ):
            problems.append("each expected finding has path, lines and class")
    return problems


def cited(where):
    match = LOCATION_LINE.match(str(where or "").strip())
    return (match["path"], int(match["line"])) if match else (None, None)


def matches(item, path, line):
    if path is None or not (path == item["path"] or item["path"].endswith("/" + path)):
        return False
    return any(abs(line - n) <= WINDOW for n in item["lines"])


def score(case, session_root):
    """A standing flag is one the walk would put in front of a reviewer; a demoted one is not."""
    session, beats = SessionStore.detached_presentation_snapshot(session_root)
    target = session.get("target") or {}
    if (target.get("repo"), target.get("number"), target.get("head_sha")) != (
        case["repo"], case["number"], case["head_sha"]
    ):
        raise CaseError("the session did not walk this case's pull request and head")
    standing = [beat for beat in beats if beat.get("state") in FLAG_STATES]
    demoted = [
        beat for beat in beats
        if (beat.get("second_read") or {}).get("verdict") == "disputed"
    ]
    found, missed = [], []
    for item in case["expected"]:
        hit = next((b for b in standing if matches(item, *cited(b.get("where")))), None)
        (found if hit else missed).append(
            dict(item, beat=hit["n"]) if hit else item
        )
    false_alarms = [
        {"n": beat["n"], "where": beat.get("where"), "claim": beat.get("claim")}
        for beat in standing
        if not any(matches(item, *cited(beat.get("where"))) for item in case["expected"])
    ]
    return {
        "case": case["id"],
        "kind": case["kind"],
        "beats": len(beats),
        "flags": len(standing),
        "demoted": len(demoted),
        "found": found,
        "missed": missed,
        "false_alarms": false_alarms,
        "pass": not missed and (case["kind"] == "miss" or not standing),
    }


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: score.py <case.json> <session-dir>", file=sys.stderr)
        return 1
    try:
        result = score(load_case(argv[0]), argv[1])
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"score: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["pass"] else 2


if __name__ == "__main__":
    sys.exit(main())
