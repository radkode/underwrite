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


SESSION_COMMANDS = (
    "init", "snapshot-pr", "check-controller", "freeze-execution", "check-execution",
    "trusted-context", "diff", "context-log", "read-blob", "check-pr", "put-session",
    "patch-session", "put-beat", "get-session", "get-beat", "history", "export", "reconcile",
)


def allowed_tools(skill, reviews, checkout):
    sessionctl = skill / "scripts" / "sessionctl.py"
    scratch = f"/{reviews}/**"
    reads = [f"{tool}(/{root}/**)" for root in (skill, reviews, checkout) for tool in ("Read", "Grep", "Glob")]
    # Every session argument has to sit under the scratch reviews root, so no real session moves.
    store = [
        f"Bash({prefix}{sessionctl} {name} {reviews}/*)"
        for prefix in ("", "python3 ")
        for name in SESSION_COMMANDS
    ]
    return [
        *reads, "Agent",
        f"Write({scratch})",
        f"Edit({scratch})",
        *store,
        f"Bash({sessionctl} --help)",
        "Bash(gh pr view *)",
        "Bash(git remote *)",
        "Bash(gh issue view *)",
        "Bash(git status *)",
        "Bash(git rev-parse *)",
        "Bash(git log *)",
    ]


def briefing(skill, reviews, checkout):
    """What the walk may do, said up front: a walk that meets an unexplained denial gives up."""
    return (
        "This is a headless eval run of underwrite in eval mode, with nobody to answer. "
        f"The skill is at {skill}, the reviewed checkout at {checkout}, and "
        f"UNDERWRITE_REVIEWS is {reviews}. Permissions match the literal command text, so "
        "write absolute paths, never shell variables. Allowed: "
        f"{skill / 'scripts' / 'sessionctl.py'} <command> {reviews}/<session> ...; "
        "gh pr view and gh issue view; git status, rev-parse, log and remote; Read, Grep and "
        "Glob inside those three directories; Write and Edit only under the reviews directory; "
        "and the subagent tool for the second read. sessionctl.py init creates the session "
        "directory, so no mkdir is needed. Anything else is denied by design and is not a "
        "reason to stop: carry on with what is allowed."
    )


def command(case, plugin, reviews, checkout, args):
    skill = plugin / "skills" / "underwrite"
    argv = [
        args.claude, "-p", f"/underwrite:underwrite {case['number']} --eval",
        "--plugin-dir", str(plugin),
        "--permission-mode", "dontAsk",
        "--allowedTools", *allowed_tools(skill, reviews, checkout),
        "--append-system-prompt", briefing(skill, reviews, checkout),
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
    root = Path(args.work_root).resolve() if args.work_root else None
    if root:
        root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"underwrite-eval-{case['id']}-", dir=root)).resolve()
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
            command(case, ROOT, reviews, checkout, args), cwd=checkout, capture_output=True, text=True,
            env=dict(os.environ, UNDERWRITE_REVIEWS=str(reviews), UNDERWRITE_CONFINE=str(reviews)),
            timeout=args.timeout,
            stdin=subprocess.DEVNULL,
        )
        (work / "walk.json").write_text(walk.stdout, encoding="utf-8")
        (work / "walk.stderr").write_text(walk.stderr, encoding="utf-8")
        try:
            transcript = json.loads(walk.stdout)
            result["cost_usd"] = transcript.get("total_cost_usd")
        except json.JSONDecodeError:
            pass
        if walk.returncode != 0:
            result["error"] = f"walk exited {walk.returncode}; see walk.stderr"
            return result
        sessions = [path for path in reviews.iterdir() if (path / "session.sqlite3").exists()]
        if len(sessions) != 1:
            result["error"] = f"walk exited {walk.returncode} and left {len(sessions)} sessions"
            return result
        result.update(score(case, sessions[0]))
    except (subprocess.SubprocessError, OSError, CaseError) as error:
        result["error"] = str(error)
    finally:
        # A scored failure is the run worth reading, so only a pass is cleaned away.
        if not args.keep and result.get("pass"):
            shutil.rmtree(work, ignore_errors=True)
            result.pop("work")
    return result


def repeated(case, args):
    """One case walked `runs` times. Walks are not deterministic, so a case passes on a
    majority of its runs and its counts are the mean, not one lucky or unlucky sample."""
    runs = [run_case(case, args) for _ in range(args.runs)]
    if len(runs) == 1:
        return runs[0]
    scored = [r for r in runs if not r.get("error")]
    passes = sum(1 for r in scored if r.get("pass"))
    result = {
        "case": case["id"],
        "runs": runs,
        "passes": passes,
        "pass_rate": round(passes / len(runs), 3),
        "pass": passes * 2 > len(runs),
        "finished": bool(scored) and all(r.get("finished") for r in scored),
        "missed": mean_count(runs, "missed"),
        "false_alarms": mean_count(runs, "false_alarms"),
        "cost_usd": round(sum(r.get("cost_usd") or 0 for r in runs), 4),
    }
    errors = [r["error"] for r in runs if r.get("error")]
    if errors:
        result["error"] = f"{len(errors)} of {len(runs)} runs errored: {errors[0]}"
    return result


def mean_count(runs, key):
    return round(sum(count(r.get(key)) for r in runs) / len(runs), 3)


def count(value):
    return value if isinstance(value, (int, float)) else len(value or ())


def provenance(args):
    def git(*argv):
        try:
            return subprocess.run(
                ["git", "-C", str(ROOT), *argv], check=True, capture_output=True, text=True
            ).stdout.strip()
        except (subprocess.SubprocessError, OSError):
            return None

    return {
        "skill_commit": git("rev-parse", "HEAD"),
        "skill_dirty": bool(git("status", "--porcelain", "--", "skills", "evals")),
        "model": args.model,
        "runs": args.runs,
        "bare": args.bare,
        "max_budget_usd": args.max_budget_usd,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(prog="run.py")
    ap.add_argument("cases", nargs="*", help="case ids; all cases when omitted")
    ap.add_argument("--model")
    ap.add_argument("--max-budget-usd", type=float, default=5.0)
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--claude", default="claude")
    ap.add_argument("--bare", action="store_true", help="skip hooks and CLAUDE.md; needs ANTHROPIC_API_KEY")
    ap.add_argument("--keep", action="store_true", help="keep every work directory, passes too")
    ap.add_argument("--runs", type=int, default=1, help="walks per case; a case passes on a majority")
    ap.add_argument("--work-root", help="put work directories here instead of the system temp")
    ap.add_argument("--baseline", type=Path, help="fail only on a regression against this result file")
    args = ap.parse_args(argv)
    if args.runs < 1:
        ap.error("--runs must be at least 1")
    paths = sorted((HERE / "cases").glob("*.json"))
    if args.cases:
        paths = [path for path in paths if path.stem in args.cases]
        unknown = set(args.cases) - {path.stem for path in paths}
        if unknown:
            ap.error(f"unknown case {', '.join(sorted(unknown))}")
    results = [repeated(load_case(path), args) for path in paths]
    summary = {
        "passed": sum(1 for r in results if r.get("pass")),
        "cases": len(results),
        "missed": round(sum(count(r.get("missed")) for r in results), 3),
        "false_alarms": round(sum(count(r.get("false_alarms")) for r in results), 3),
    }
    report = {"summary": summary, "provenance": provenance(args), "results": results}
    if args.baseline:
        report["regressions"] = regressions(json.loads(args.baseline.read_text(encoding="utf-8")), report)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 2 if report["regressions"] else 0
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if summary["passed"] == summary["cases"] else 2


def regressions(baseline, report):
    """What got worse: a case that errored, did not finish, went missing, or passed and now
    fails, or more misses or false alarms. An unscored case never counts as holding steady."""
    before = {r["case"]: r for r in baseline.get("results", ())}
    now = {r["case"]: r for r in report["results"]}
    worse = [f"{case} is in the baseline and was not run" for case in sorted(set(before) - set(now))]
    for r in report["results"]:
        if r.get("error"):
            worse.append(f"{r['case']} errored: {r['error']}")
        elif not r.get("finished"):
            worse.append(f"{r['case']} did not finish its walk")
        elif before.get(r["case"], {}).get("pass") and not r.get("pass"):
            worse.append(f"{r['case']} passed in the baseline and now fails")
    for key in ("missed", "false_alarms"):
        if report["summary"][key] > baseline.get("summary", {}).get(key, 0):
            worse.append(f"{key} rose from {baseline['summary'].get(key, 0)} to {report['summary'][key]}")
    return worse


if __name__ == "__main__":
    sys.exit(main())
