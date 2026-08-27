"""Capture and guard one exact GitHub pull request snapshot."""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from session_store import Conflict, StoreError


_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


class SnapshotError(StoreError):
    pass


class TargetMoved(Conflict):
    pass


def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SnapshotError(f"{name} must be a positive integer")
    return value


def _repo(value):
    if not isinstance(value, str) or not _REPO.fullmatch(value):
        raise SnapshotError("repo must be owner/name")
    return value


def _run(command, cwd=None, stdout=None, env=None):
    try:
        completed = subprocess.run(
            command,
            cwd=None if cwd is None else str(cwd),
            stdout=subprocess.PIPE if stdout is None else stdout,
            stderr=subprocess.PIPE,
            env=env,
        )
    except OSError as error:
        raise SnapshotError(f"could not run {command[0]}: {error}") from error
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        if not detail:
            detail = f"exit {completed.returncode}"
        raise SnapshotError(f"{command[0]} failed: {detail}")
    return completed.stdout


def load_pr(repo, number):
    repo, number = _repo(repo), _positive(number, "PR number")
    raw = _run([
        "gh",
        "api",
        "-H",
        "Accept: application/vnd.github+json",
        f"repos/{repo}/pulls/{number}",
    ])
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise SnapshotError("GitHub returned malformed PR metadata") from error
    if not isinstance(value, dict):
        raise SnapshotError("GitHub returned non-object PR metadata")
    return value


def _field(document, path, expected=None):
    value = document
    for name in path:
        if not isinstance(value, dict) or name not in value:
            raise SnapshotError(f"PR metadata has no {'.'.join(path)}")
        value = value[name]
    if expected is not None and not isinstance(value, expected):
        raise SnapshotError(f"PR metadata {'.'.join(path)} has the wrong type")
    return value


def _identity(metadata):
    merged_at = metadata.get("merged_at")
    if merged_at is not None and not isinstance(merged_at, str):
        raise SnapshotError("PR metadata merged_at has the wrong type")
    state = _field(metadata, ("state",), str)
    if state not in ("open", "closed"):
        raise SnapshotError(f"PR metadata has unsupported state {state!r}")
    number = _positive(_field(metadata, ("number",)), "PR metadata number")
    head_repo = _field(metadata, ("head", "repo"))
    if head_repo is None:
        head_repo_id = None
        head_repo_name = None
    elif isinstance(head_repo, dict):
        head_repo_id = _positive(
            _field(head_repo, ("id",)), "PR metadata head.repo.id"
        )
        head_repo_name = _repo(_field(head_repo, ("full_name",), str))
    else:
        raise SnapshotError("PR metadata head.repo has the wrong type")
    head_ref = _field(metadata, ("head", "ref"), str)
    if not head_ref.strip():
        raise SnapshotError("PR metadata head.ref must be non-empty")
    identity = {
        "repo": _repo(_field(metadata, ("base", "repo", "full_name"), str)),
        "number": number,
        "state": state,
        "merged_at": merged_at,
        "base_sha": _field(metadata, ("base", "sha"), str),
        "head_sha": _field(metadata, ("head", "sha"), str),
        "head_repo_id": head_repo_id,
        "head_repo": head_repo_name,
        "head_ref": head_ref,
    }
    for name in ("base_sha", "head_sha"):
        if not _FULL_SHA.fullmatch(identity[name]):
            raise SnapshotError(f"PR metadata {name} is not a full lowercase SHA")
    return identity


def _same_identity(left, right):
    return _identity(left) == _identity(right)


def _git(command, repo, safe=False, stdout=None):
    prefix = ["git", "--no-replace-objects"]
    if safe:
        prefix.extend([
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.pager=cat",
            "-c",
            "core.quotePath=true",
        ])
    env = dict(
        os.environ,
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_TERMINAL_PROMPT="0",
    )
    return _run(prefix + ["-C", str(repo)] + command, stdout=stdout, env=env)


def _commit(repo, ref):
    return _git(
        ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        repo,
        safe=True,
    ).decode("ascii").strip()


def _diff_command(mode):
    command = [
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--find-renames=50%",
    ]
    if mode == "names":
        command.extend(["--name-only", "-z"])
    else:
        command.extend([
            "--binary",
            "--full-index",
            "--diff-algorithm=myers",
            "--no-indent-heuristic",
            "--unified=3",
            "--src-prefix=a/",
            "--dst-prefix=b/",
        ])
    return command + ["refs/underwrite/base...refs/underwrite/head", "--"]


def capture(store, repo, number, api=load_pr):
    requested_repo, number = _repo(repo), _positive(number, "PR number")
    first = api(requested_repo, number)
    identity = _identity(first)
    if identity["repo"].lower() != requested_repo.lower():
        raise SnapshotError(
            f"PR belongs to {identity['repo']}, not {requested_repo}"
        )
    if identity["number"] != number:
        raise SnapshotError(
            f"GitHub returned PR {identity['number']}, not {number}"
        )

    clone_url = _field(first, ("base", "repo", "clone_url"), str)
    changed_files = _field(first, ("changed_files",))
    if isinstance(changed_files, bool) or not isinstance(changed_files, int) or changed_files < 0:
        raise SnapshotError("PR metadata changed_files must be a non-negative integer")

    with tempfile.TemporaryDirectory(prefix="underwrite-pr-") as temporary:
        temporary = Path(temporary)
        bare = temporary / "review.git"
        _run(["git", "init", "--bare", "--template=", str(bare)])
        _git(
            [
                "fetch",
                "--atomic",
                "--no-tags",
                "--recurse-submodules=no",
                clone_url,
                f"+{identity['base_sha']}:refs/underwrite/base",
                f"+refs/pull/{number}/head:refs/underwrite/head",
            ],
            bare,
            safe=True,
        )

        actual_base = _commit(bare, "refs/underwrite/base")
        actual_head = _commit(bare, "refs/underwrite/head")
        if actual_base != identity["base_sha"] or actual_head != identity["head_sha"]:
            raise TargetMoved("PR base or head moved while its snapshot was fetched")

        merge_bases = _git(
            ["merge-base", "--all", "refs/underwrite/base", "refs/underwrite/head"],
            bare,
            safe=True,
        ).decode("ascii").splitlines()
        if len(merge_bases) != 1:
            raise SnapshotError(
                f"PR target has {len(merge_bases)} merge bases; expected exactly one"
            )
        merge_base = merge_bases[0]

        names = _git(_diff_command("names"), bare, safe=True)
        local_files = names.count(b"\0")
        if local_files != changed_files:
            raise SnapshotError(
                f"GitHub reports {changed_files} changed files; local diff has {local_files}"
            )

        diff = temporary / "pr.diff"
        with diff.open("wb") as handle:
            _git(_diff_command("patch"), bare, safe=True, stdout=handle)
            handle.flush()
            os.fsync(handle.fileno())

        second = api(requested_repo, number)
        if not _same_identity(first, second):
            raise TargetMoved("PR base, head, or lifecycle moved during snapshot capture")

        metadata = temporary / "pr.json"
        metadata.write_text(
            json.dumps(first, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        with metadata.open("rb") as handle:
            os.fsync(handle.fileno())

        target = {
            "version": 1,
            "kind": "github_pr",
            "repo": identity["repo"],
            "number": number,
            "state": identity["state"],
            "merged_at": identity["merged_at"],
            "base_sha": identity["base_sha"],
            "head_sha": identity["head_sha"],
            "head_repo_id": identity["head_repo_id"],
            "head_repo": identity["head_repo"],
            "head_ref": identity["head_ref"],
            "merge_base_sha": merge_base,
            "changed_files": changed_files,
        }
        return store.freeze_target(target, diff, metadata)


def check(store, require_open=False, api=load_pr):
    target = store.verify_target_files()
    if target["kind"] != "github_pr":
        raise SnapshotError("frozen target is not a GitHub pull request")
    current = _identity(api(target["repo"], target["number"]))
    expected = {
        name: target[name]
        for name in (
            "repo",
            "number",
            "state",
            "merged_at",
            "base_sha",
            "head_sha",
            "head_repo_id",
            "head_repo",
            "head_ref",
        )
    }
    moved = [name for name in expected if current[name] != expected[name]]
    if moved:
        raise TargetMoved(f"frozen PR target moved: {', '.join(moved)} changed")
    if require_open and (current["state"] != "open" or current["merged_at"] is not None):
        raise TargetMoved("frozen PR target is not open")
    return target


def _branch(repo):
    try:
        return _git(["symbolic-ref", "--quiet", "--short", "HEAD"], repo, safe=True).decode(
            "utf-8"
        ).strip()
    except SnapshotError as error:
        raise Conflict("worktree HEAD must be attached to a branch") from error


def _parents(repo, commit):
    raw = _git(["cat-file", "-p", commit], repo, safe=True)
    headers = raw.split(b"\n\n", 1)[0].splitlines()
    if not headers or not re.fullmatch(rb"tree [0-9a-f]{40}", headers[0]):
        raise Conflict(f"object {commit} is not a commit")
    parents = []
    for header in headers[1:]:
        if header.startswith(b"parent "):
            parent = header[7:]
            if not re.fullmatch(rb"[0-9a-f]{40}", parent):
                raise Conflict(f"commit {commit} has a malformed parent")
            parents.append(parent.decode("ascii"))
    return parents


def _check_route(position, branch):
    target = position["target"]
    deliveries = position["deliveries"]
    if target["state"] == "open" and target["merged_at"] is None:
        if target["head_repo"] is None:
            raise Conflict("open PR has no head repository for branch delivery")
        expected_branch = target["head_ref"]
    else:
        expected_branch = position.get("delivery_branch")
        if not expected_branch:
            raise Conflict("merged PR delivery branch has not been pinned")
    if not expected_branch or branch != expected_branch:
        raise Conflict(
            f"worktree branch {branch!r} does not match delivery branch {expected_branch!r}"
        )
    for delivery in deliveries:
        if delivery.get("branch") != expected_branch:
            raise Conflict("recorded commit deliveries do not share one branch")
    return expected_branch


def _check_recorded_chain(position, repo_root):
    previous = position["target"]["head_sha"]
    for delivery in position["deliveries"]:
        artifact = delivery.get("artifact")
        if not isinstance(artifact, str) or not _FULL_SHA.fullmatch(artifact):
            raise Conflict("recorded PR commit is not a full lowercase SHA")
        if _parents(repo_root, artifact) != [previous]:
            raise Conflict("recorded PR commits are not a linear chain from the frozen head")
        previous = artifact
    if previous != position["expected_head"]:
        raise Conflict("recorded PR commit position is inconsistent")


def check_worktree(store, repo_root):
    store.verify_target_files()
    position = store.branch_position()
    repo_root = Path(repo_root).expanduser()
    _check_recorded_chain(position, repo_root)
    head = _commit(repo_root, "HEAD")
    branch = _branch(repo_root)
    _check_route(position, branch)
    if head != position["expected_head"]:
        raise Conflict(
            f"worktree HEAD {head} does not match recorded position "
            f"{position['expected_head']}"
        )
    return {
        "branch": branch,
        "head_sha": head,
        "head_repo": position["target"]["head_repo"],
        "head_ref": position["target"]["head_ref"],
    }


def check_commit(store, repo_root, seq, beat_n, artifact, branch):
    position = store.branch_position()
    for delivery in position["deliveries"]:
        if delivery["cause_seq"] == seq or delivery["beat_n"] == beat_n:
            if (
                delivery["cause_seq"],
                delivery["beat_n"],
                delivery["artifact"],
                delivery.get("branch"),
            ) != (seq, beat_n, artifact, branch):
                raise Conflict("commit delivery retry does not match its recorded receipt")
            return {"replay": True, "head_sha": artifact, "branch": branch}
    if not isinstance(artifact, str) or not _FULL_SHA.fullmatch(artifact):
        raise Conflict("PR commit delivery requires a full lowercase SHA")
    repo_root = Path(repo_root).expanduser()
    _check_recorded_chain(position, repo_root)
    head = _commit(repo_root, "HEAD")
    current_branch = _branch(repo_root)
    expected_branch = _check_route(position, current_branch)
    if branch != expected_branch:
        raise Conflict(
            f"commit branch {branch!r} does not match worktree branch {expected_branch!r}"
        )
    if head != artifact:
        raise Conflict(f"commit artifact {artifact} is not worktree HEAD {head}")
    if _parents(repo_root, artifact) != [position["expected_head"]]:
        raise Conflict("new commit is not one commit directly on the recorded position")
    return {"replay": False, "head_sha": head, "branch": branch}


def review_identity(store):
    return {
        "commit_id": store.frozen_target()["head_sha"],
        "marker": store.review_marker(),
    }


def _review_url(response):
    url = response.get("html_url")
    if not isinstance(url, str) or not url.strip():
        links = response.get("_links")
        url = (links.get("html") or {}).get("href") if isinstance(links, dict) else None
    if not isinstance(url, str) or not url.strip():
        raise SnapshotError("review response has no URL")
    return url


def _matching_review(response, target, marker, actor):
    user = response.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    body = response.get("body")
    return (
        response.get("commit_id") == target["head_sha"]
        and isinstance(body, str)
        and marker in body
        and isinstance(login, str)
        and login.lower() == actor.lower()
        and response.get("state")
        in ("COMMENTED", "APPROVED", "CHANGES_REQUESTED", "DISMISSED")
    )


def review_receipt(store, source, actor):
    target = store.frozen_target()
    if not isinstance(actor, str) or not actor.strip():
        raise SnapshotError("review actor must be non-empty text")
    actor = actor.strip()
    marker = store.review_marker()
    try:
        with Path(source).open(encoding="utf-8") as handle:
            response = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise SnapshotError(f"could not read review response: {error}") from error
    candidates = response if isinstance(response, list) else [response]
    if candidates and all(isinstance(page, list) for page in candidates):
        candidates = [item for page in candidates for item in page]
    if not all(isinstance(item, dict) for item in candidates):
        raise SnapshotError("review response must be an object or an array of objects")
    matches = [
        item for item in candidates if _matching_review(item, target, marker, actor)
    ]
    if len(matches) != 1:
        raise Conflict(
            "expected exactly one review matching the frozen commit, marker, and actor; "
            f"found {len(matches)}"
        )
    match = matches[0]
    result = {
        "commit_id": target["head_sha"],
        "marker": marker,
        "state": match["state"],
        "url": _review_url(match),
    }
    if isinstance(match.get("id"), int):
        result["review_id"] = match["id"]
    return result
