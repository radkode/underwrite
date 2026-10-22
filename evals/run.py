#!/usr/bin/env python3
"""Replay eval cases through a headless walk and score each one.

Every case is a merged pull request, walked from a fresh clone at its base with the skill
from this checkout. Nothing is posted: a merged PR freezes to report audience and eval mode
delivers nothing. Each run spends model budget, so it is a local gate, not a CI job.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from score import CaseError, load_case, score  # noqa: E402


def allowed_tools(skill):
    sessionctl = skill / "scripts" / "sessionctl.py"
    return [
        "Read", "Grep", "Glob", "Agent",
        f"Bash({sessionctl} *)",
        f"Bash(python3 {sessionctl} *)",
        "Bash(gh pr view *)",
        "Bash(gh issue view *)",
        "Bash(mkdir -p *)",
        "Bash(git status *)",
        "Bash(git rev-parse *)",
        "Bash(git log *)",
    ]


def command(case, plugin, args):
    argv = [
        args.claude, "-p", f"/underwrite:underwrite {case['number']} --eval",
        "--plugin-dir", str(plugin),
        "--permission-mode", "dontAsk",
        "--allowedTools", *allowed_tools(plugin / "skills" / "underwrite"),
        "--output-format", "json",
        "--no-session-persistence",
        "--max-budget-usd", str(args.max_budget_usd),
    ]
    if args.model:
        argv += ["--model", args.model]
    if args.bare:
        argv.append("--bare")
    return argv


def run_case(case, args):
    work = Path(tempfile.mkdtemp(prefix=f"underwrite-eval-{case['id']}-"))
    checkout, reviews = work / "checkout", work / "reviews"
    reviews.mkdir()
    result = {"case": case["id"], "work": str(work)}
    try:
        subprocess.run(
            ["gh", "repo", "clone", case["repo"], str(checkout), "--", "--quiet"],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "-C", str(checkout), "checkout", "--quiet", "--detach", case["base_sha"]],
            check=True, capture_output=True, text=True,
        )
        walk = subprocess.run(
            command(case, ROOT, args), cwd=checkout, capture_output=True, text=True,
            env=dict(os.environ, UNDERWRITE_REVIEWS=str(reviews)), timeout=args.timeout,
            stdin=subprocess.DEVNULL,
        )
        (work / "walk.json").write_text(walk.stdout, encoding="utf-8")
        (work / "walk.stderr").write_text(walk.stderr, encoding="utf-8")
        try:
            transcript = json.loads(walk.stdout)
            result["cost_usd"] = transcript.get("total_cost_usd")
        except json.JSONDecodeError:
            pass
        sessions = [path for path in reviews.iterdir() if (path / "session.sqlite3").exists()]
        if len(sessions) != 1:
            result["error"] = f"walk exited {walk.returncode} and left {len(sessions)} sessions"
            return result
        result.update(score(case, sessions[0]))
    except (subprocess.SubprocessError, OSError, CaseError) as error:
        result["error"] = str(error)
    finally:
        if not args.keep and "error" not in result:
            shutil.rmtree(work, ignore_errors=True)
            result.pop("work")
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(prog="run.py")
    ap.add_argument("cases", nargs="*", help="case ids; all cases when omitted")
    ap.add_argument("--model")
    ap.add_argument("--max-budget-usd", type=float, default=5.0)
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--claude", default="claude")
    ap.add_argument("--bare", action="store_true", help="skip hooks and CLAUDE.md; needs ANTHROPIC_API_KEY")
    ap.add_argument("--keep", action="store_true", help="keep each work directory")
    args = ap.parse_args(argv)
    paths = sorted((HERE / "cases").glob("*.json"))
    if args.cases:
        paths = [path for path in paths if path.stem in args.cases]
        unknown = set(args.cases) - {path.stem for path in paths}
        if unknown:
            ap.error(f"unknown case {', '.join(sorted(unknown))}")
    results = [run_case(load_case(path), args) for path in paths]
    summary = {
        "passed": sum(1 for r in results if r.get("pass")),
        "cases": len(results),
        "missed": sum(len(r.get("missed", ())) for r in results),
        "false_alarms": sum(len(r.get("false_alarms", ())) for r in results),
    }
    print(json.dumps({"summary": summary, "results": results}, indent=2, sort_keys=True))
    return 0 if summary["passed"] == summary["cases"] else 2


if __name__ == "__main__":
    sys.exit(main())
