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

# A dropped flag was put in front of the reviewer and refused, so it no longer stands.
STANDING_STATES = tuple(state for state in FLAG_STATES if state != "dropped")

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
    for field, kind in (("id", str), ("repo", str), ("evidence", str)):
        if not isinstance(case[field], kind) or not case[field].strip():
            problems.append(f"{field} must be nonempty text")
    if isinstance(case["number"], bool) or not isinstance(case["number"], int) or case["number"] < 1:
        problems.append("number must be a positive integer")
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
            and isinstance(item.get("keywords"), list)
            and item["keywords"]
            and all(isinstance(k, str) and k.strip() for k in item["keywords"])
        ):
            problems.append("each expected finding has path, lines, class and keywords")
    return problems


def cited(where):
    match = LOCATION_LINE.match(str(where or "").strip())
    return (match["path"], int(match["line"])) if match else (None, None)


def words(beat):
    slots = beat.get("slots") if isinstance(beat.get("slots"), dict) else {}
    return " ".join(str(v) for v in (beat.get("claim"), *slots.values()) if v).lower()


def matches(item, beat):
    """Near the defect and about it: the flag's own words name at least one of the class's
    keywords, so a different finding that happens to sit close by earns nothing."""
    path, line = cited(beat.get("where"))
    if path is None or not (path == item["path"] or item["path"].endswith("/" + path)):
        return False
    if not any(abs(line - n) <= WINDOW for n in item["lines"]):
        return False
    text = words(beat)
    return any(keyword.lower() in text for keyword in item["keywords"])


def score(case, session_root):
    """A standing flag is one the walk would put in front of a reviewer; a demoted one is not.
    A case passes when every expected finding is found and nothing else stands, and only for
    a finished walk: at least one beat, a recommendation, and the eval_complete marker."""
    session, beats = SessionStore.detached_presentation_snapshot(session_root)
    target = session.get("target") or {}
    if (
        target.get("repo"), target.get("number"), target.get("base_sha"), target.get("head_sha")
    ) != (case["repo"], case["number"], case["base_sha"], case["head_sha"]):
        raise CaseError("the session did not walk this case's pull request, base and head")
    standing = [beat for beat in beats if beat.get("state") in STANDING_STATES]
    demoted = [
        beat for beat in beats
        if (beat.get("second_read") or {}).get("verdict") == "disputed"
    ]
    found, missed, used = [], [], set()
    for item in case["expected"]:
        hit = next(
            (b for b in standing if b["n"] not in used and matches(item, b)),
            None,
        )
        if hit:
            used.add(hit["n"])
            found.append(dict(item, beat=hit["n"]))
        else:
            missed.append(item)
    false_alarms = [
        {"n": beat["n"], "where": beat.get("where"), "claim": beat.get("claim")}
        for beat in standing
        if beat["n"] not in used
    ]
    # A recommendation is written provisionally before the walk, so only the marker eval mode
    # writes last says the walk reached its end.
    finished = (
        bool(beats)
        and bool(str(session.get("recommendation") or "").strip())
        and session.get("eval_complete") is True
    )
    return {
        "case": case["id"],
        "kind": case["kind"],
        "beats": len(beats),
        "flags": len(standing),
        "demoted": len(demoted),
        "found": found,
        "missed": missed,
        "false_alarms": false_alarms,
        "finished": finished,
        "pass": finished and not missed and not false_alarms,
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
