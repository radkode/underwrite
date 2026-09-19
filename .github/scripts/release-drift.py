#!/usr/bin/env python3
"""Report when main carries shipped behavior no published version reaches.

`claude plugin update` compares version strings, so behavior that lands on main
without a version bump is unreachable from every install. It has happened in four
release cycles (0.4.0, 0.5.0, 0.6.0, 0.7.0), the worst of them for 16.6 days.

  release-drift.py                 print the verdict
  release-drift.py --reconcile     also open, update or close the tracking issue

Exit 0 whether or not main is drifting: this reports, it never blocks.
Exit 1 on a usage or git error.
"""

import argparse
import json
import os
import subprocess
import sys
import time


MANIFEST = ".claude-plugin/plugin.json"
# gateway/ is not declared in plugin.json but implementationctl imports it, and
# docs/host-execution-protocol.md is normative and cited from SKILL.md.
PUBLISHED = ("skills", "gateway", "docs")
LIMIT = 6
STALE_DAYS = 7
LABEL = "release-drift"
TITLE = "main is ahead of the published plugin version"


class DriftError(Exception):
    pass


def git(*args):
    out = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False
    )
    if out.returncode:
        raise DriftError(f"git {' '.join(args)}: {out.stderr.strip()}")
    return out.stdout


def published_version(sha):
    out = subprocess.run(
        ["git", "show", f"{sha}:{MANIFEST}"], capture_output=True, text=True
    )
    if out.returncode:
        return None
    try:
        return json.loads(out.stdout).get("version")
    except json.JSONDecodeError:
        return None


def last_release(head="HEAD"):
    """The newest commit whose published version differs from its parent's.

    Two commits in this repo edited the manifests without bumping anything, so
    "touched .claude-plugin/" would read either of them as a release.
    """
    for sha in git("rev-list", "--first-parent", head, "--", MANIFEST).split():
        parents = git("rev-list", "--parents", "-1", sha).split()[1:]
        if not parents:
            return sha
        if published_version(sha) != published_version(parents[0]):
            return sha
    raise DriftError("no commit in this history sets a plugin version")


def unreleased(base, head="HEAD"):
    """Merged changes to the published tree since base, oldest first."""
    log = git(
        "log",
        "--first-parent",
        "--reverse",
        "--format=%H%x00%ct%x00%s",
        f"{base}..{head}",
        "--",
        *PUBLISHED,
    )
    rows = []
    for line in log.splitlines():
        if line.strip():
            sha, stamp, subject = line.split("\0", 2)
            rows.append({"sha": sha, "stamp": int(stamp), "subject": subject})
    return rows


def verdict(head="HEAD", now=None):
    if git("rev-parse", "--is-shallow-repository").strip() == "true":
        raise DriftError("shallow clone: this needs fetch-depth 0 to see history")
    now = time.time() if now is None else now
    base = last_release(head)
    changes = unreleased(base, head)
    age = (now - changes[0]["stamp"]) / 86400.0 if changes else 0.0
    return {
        "version": published_version(base),
        "released_at": int(git("log", "-1", "--format=%ct", base).strip()),
        "base": base,
        "changes": changes,
        "age_days": age,
        "drifting": len(changes) >= LIMIT or age >= STALE_DAYS,
    }


def summarize(state):
    count = len(state["changes"])
    if not state["drifting"]:
        return f"{count} unreleased changes on {state['version']}, within limits"
    return (
        f"{count} unreleased changes on {state['version']}, "
        f"oldest {state['age_days']:.1f} days: cut a release"
    )


def body(state, repo):
    url = f"https://github.com/{repo}/commit/"
    lines = [
        f"`main` carries {len(state['changes'])} changes to the published tree that "
        f"no released version reaches.",
        "",
        f"Both manifests still say **{state['version']}**, published "
        f"{time.strftime('%Y-%m-%d', time.gmtime(state['released_at']))} in "
        f"[`{state['base'][:7]}`]({url}{state['base']}), so `claude plugin update` "
        f"is a no-op for everything below.",
        "",
        f"Oldest unreleased change: **{state['age_days']:.1f} days** old.",
        "",
        "| merged | commit | change |",
        "| --- | --- | --- |",
    ]
    for change in reversed(state["changes"]):
        day = time.strftime("%Y-%m-%d", time.gmtime(change["stamp"]))
        short = change["sha"][:7]
        subject = change["subject"].replace("|", "\\|")
        lines.append(f"| {day} | [`{short}`]({url}{change['sha']}) | {subject} |")
    lines += [
        "",
        f"To clear this, bump `version` in `{MANIFEST}` and "
        "`.claude-plugin/marketplace.json` to the same new value and merge. This "
        "issue closes itself on the next run after that lands.",
    ]
    return "\n".join(lines)


def gh(*args, **kwargs):
    out = subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=False, **kwargs
    )
    if out.returncode:
        raise DriftError(f"gh {' '.join(args)}: {out.stderr.strip()}")
    return out.stdout


def tracking_issue(repo):
    found = json.loads(
        gh(
            "issue",
            "list",
            "--repo",
            repo,
            "--label",
            LABEL,
            "--state",
            "all",
            "--limit",
            "1",
            "--json",
            "number,state",
        )
    )
    return found[0] if found else None


def reconcile(state, repo):
    """One issue per repo, rewritten in place so a long drift sends one notification."""
    issue = tracking_issue(repo)
    text = body(state, repo)
    if state["drifting"]:
        if issue is None:
            # --force so the label is created here rather than as repo config no
            # reviewer sees, and re-running never fails on an existing one.
            gh("label", "create", LABEL, "--repo", repo, "--force",
               "--color", "d4c5f9", "--description", "main is ahead of its published version")
            gh("issue", "create", "--repo", repo, "--title", TITLE,
               "--label", LABEL, "--body", text)
            return "opened"
        if issue["state"] == "CLOSED":
            gh("issue", "reopen", "--repo", repo, str(issue["number"]))
        gh("issue", "edit", "--repo", repo, str(issue["number"]), "--body", text)
        return "reopened" if issue["state"] == "CLOSED" else "updated"
    if issue is not None and issue["state"] == "OPEN":
        gh("issue", "close", "--repo", repo, str(issue["number"]), "--comment",
           f"Released as {state['version']}. Nothing unreleased on the published tree.")
        return "closed"
    return "quiet"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reconcile", action="store_true",
                        help="open, update or close the tracking issue")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    args = parser.parse_args(argv)

    try:
        state = verdict()
        line = summarize(state)
        if args.reconcile:
            if not args.repo:
                parser.error("--reconcile needs --repo or GITHUB_REPOSITORY")
            line = f"{line} ({reconcile(state, args.repo)})"
    except DriftError as exc:
        print(f"release-drift: {exc}", file=sys.stderr)
        return 1

    print(line)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"{line}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
