"""Capture and guard one exact GitHub pull request snapshot."""

import base64
import contextlib
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from session_store import Conflict, StoreError


_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_GOVERNING_NAMES = {
    b"AGENTS.md",
    b"AGENTS.override.md",
    b"CLAUDE.md",
    b"CLAUDE.local.md",
}
_GOVERNING_GLOBS = (
    ":(glob)**/AGENTS.md",
    ":(glob)**/AGENTS.override.md",
    ":(glob)**/CLAUDE.md",
    ":(glob)**/CLAUDE.local.md",
    ":(glob)**/.claude",
    ":(glob)**/.claude/rules",
    ":(glob)**/.claude/rules/**",
    ":(glob)**/.claude/rules/**/*.md",
)
_FILTER_COMMAND = re.compile(rb"filter\..+\.(?:clean|process)", re.IGNORECASE)
_PROMISOR_REMOTE = re.compile(
    rb"remote\..+\.(?:partialclonefilter|promisor)", re.IGNORECASE
)
_IMPORT = re.compile(
    r"(?<![\w@])@(?![A-Za-z][A-Za-z0-9+.-]*://)"
    r"(?P<path>(?:~|/|\.\.?/)?[\w.+-]+(?:/[\w.+-]+)*)"
)
_MAX_TRUSTED_FILES = 256
_MAX_TRUSTED_BYTES = 2_000_000
_MAX_IMPORT_DEPTH = 4
_MAX_BLOB_BYTES = 10_000_000
_MAX_CONTEXT_COMMITS = 100
_MAX_CONTROLLER_REPOSITORIES = 256


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


def _git_environment(literal_paths=True):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_LITERAL_PATHSPECS": "1" if literal_paths else "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
    })
    return env


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


def _git(command, repo, safe=False, stdout=None, literal_paths=True):
    prefix = ["git", "--no-replace-objects"]
    if safe:
        prefix.extend([
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.pager=cat",
            "-c",
            "core.quotePath=true",
        ])
    return _run(
        prefix + ["-C", str(repo)] + command,
        stdout=stdout,
        env=_git_environment(literal_paths=literal_paths),
    )


def _commit(repo, ref):
    return _git(
        ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        repo,
        safe=True,
    ).decode("ascii").strip()


def _credential_config(clone_url):
    parsed = urlsplit(clone_url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return []
    executable = shutil.which("gh")
    if not executable:
        raise SnapshotError("gh is required to authenticate the GitHub object fetch")
    executable = str(Path(executable).resolve())
    helper = f"!{shlex.quote(executable)} auth git-credential"
    scope = f"credential.https://{parsed.hostname.lower()}.helper={helper}"
    return ["-c", "credential.helper=", "-c", scope]


def _fetch_pr_objects(repo, clone_url, identity, number):
    _git(
        _credential_config(clone_url)
        + [
            "fetch",
            "--atomic",
            "--no-tags",
            "--recurse-submodules=no",
            clone_url,
            f"+{identity['base_sha']}:refs/underwrite/base",
            f"+refs/pull/{number}/head:refs/underwrite/head",
        ],
        repo,
        safe=True,
    )


def _governing_link_path(path):
    return (
        path in (b".claude", b".claude/rules")
        or path.startswith(b".claude/rules/")
        or path.rsplit(b"/", 1)[-1] in _GOVERNING_NAMES
    )


def _head_tree(repo):
    tree = _git(
        ["ls-tree", "-r", "-z", "--full-tree", "HEAD", "--"],
        repo,
        safe=True,
    )
    entries = {}
    for record in tree.split(b"\0"):
        if not record:
            continue
        try:
            header, path = record.split(b"\t", 1)
            mode, kind, _object_id = header.split(b" ", 2)
        except ValueError as error:
            raise SnapshotError("controller tree contains a malformed entry") from error
        if mode in (b"120000", b"160000") and _governing_link_path(path):
            raise SnapshotError(
                "controller governing paths must be regular files, not symlinks or "
                "gitlinks"
            )
        if path in entries:
            raise SnapshotError("controller tree contains duplicate paths")
        entries[path] = (mode, kind, _object_id)
    return entries


def _gitlink_paths(repo):
    return [
        path
        for path, (mode, kind, _object_id) in _head_tree(repo).items()
        if mode == b"160000" and kind == b"commit"
    ]


def _controller_repositories(top):
    pending = [top]
    repositories = []
    seen = set()
    while pending:
        repo = pending.pop()
        identity = str(repo.resolve())
        if identity in seen:
            continue
        seen.add(identity)
        repositories.append(repo)
        if len(repositories) > _MAX_CONTROLLER_REPOSITORIES:
            raise SnapshotError("controller checkout has too many submodules")
        _reject_controller_config(repo)
        for raw_path in _gitlink_paths(repo):
            candidate = repo / os.fsdecode(raw_path)
            if not os.path.lexists(candidate / ".git"):
                continue
            if not candidate.is_dir() or candidate.is_symlink():
                raise SnapshotError("controller submodule worktree is not a directory")
            try:
                submodule_top = Path(
                    os.fsdecode(
                        _git(
                            ["rev-parse", "--show-toplevel"],
                            candidate,
                            safe=True,
                        ).strip()
                    )
                ).resolve()
            except (SnapshotError, UnicodeError) as error:
                raise SnapshotError(
                    "controller submodule must be a Git worktree"
                ) from error
            if submodule_top != candidate.resolve():
                raise SnapshotError("controller submodule root does not match its path")
            pending.append(submodule_top)
    return repositories


def _reject_controller_config(repo):
    records = _git(
        ["config", "--includes", "--null", "--list"],
        repo,
        safe=True,
    ).split(b"\0")
    entries = [record.partition(b"\n") for record in records if record]
    keys = [key for key, _separator, _value in entries]
    if any(_FILTER_COMMAND.fullmatch(key) for key in keys if key):
        raise SnapshotError(
            "controller checkout config defines a clean or process filter"
        )
    if any(
        key.lower() == b"extensions.partialclone"
        or _PROMISOR_REMOTE.fullmatch(key)
        for key in keys
    ):
        raise SnapshotError(
            "controller checkout cannot use a partial clone or promisor remote"
        )


def _reject_index_concealment(repo):
    entries = _git(
        ["ls-files", "-v", "-z", "--sparse"],
        repo,
        safe=True,
    ).split(b"\0")
    for entry in entries:
        if not entry:
            continue
        if len(entry) < 3 or entry[1:2] != b" ":
            raise SnapshotError("controller index contains a malformed entry")
        tag = entry[:1]
        if tag == b"S" or tag.islower():
            raise SnapshotError(
                "controller index uses assume-unchanged, skip-worktree, or sparse "
                "entries"
            )


def _index_tree(repo):
    raw = _git(
        ["ls-files", "--stage", "-z"],
        repo,
        safe=True,
    )
    entries = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            header, path = record.split(b"\t", 1)
            mode, object_id, stage = header.split(b" ", 2)
        except ValueError as error:
            raise SnapshotError("controller index contains a malformed entry") from error
        if stage != b"0" or path in entries:
            raise SnapshotError("controller index does not exactly match HEAD")
        entries[path] = (mode, object_id)
    return entries


def _blob_hasher(algorithm, size):
    try:
        digest = hashlib.new(algorithm)
    except ValueError as error:
        raise SnapshotError("controller repository uses an unsupported object format") from error
    digest.update(f"blob {size}\0".encode("ascii"))
    return digest


def _regular_blob(repo_path, executable, algorithm):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(repo_path, flags)
    except OSError as error:
        raise SnapshotError("controller tracked file cannot be read safely") from error
    with os.fdopen(descriptor, "rb") as handle:
        details = os.fstat(handle.fileno())
        if not stat.S_ISREG(details.st_mode):
            raise SnapshotError("controller tracked path is not a regular file")
        if bool(details.st_mode & stat.S_IXUSR) != executable:
            raise SnapshotError("controller tracked file mode does not match HEAD")
        digest = _blob_hasher(algorithm, details.st_size)
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest().encode("ascii")


def _symlink_blob(repo_path, algorithm):
    try:
        details = os.lstat(repo_path)
        target = os.readlink(repo_path)
    except OSError as error:
        raise SnapshotError("controller tracked symlink cannot be read safely") from error
    if not stat.S_ISLNK(details.st_mode):
        raise SnapshotError("controller tracked path is not a symlink")
    raw = os.fsencode(target)
    digest = _blob_hasher(algorithm, len(raw))
    digest.update(raw)
    return digest.hexdigest().encode("ascii")


def _verify_gitlink(repo, path, object_id):
    candidate = repo / os.fsdecode(path)
    try:
        details = os.lstat(candidate)
    except FileNotFoundError:
        return
    except OSError as error:
        raise SnapshotError("controller submodule path cannot be inspected") from error
    if not stat.S_ISDIR(details.st_mode):
        raise SnapshotError("controller submodule path must be a real directory")
    if os.path.lexists(candidate / ".git"):
        _reject_controller_config(candidate)
        if _commit(candidate, "HEAD").encode("ascii") != object_id:
            raise SnapshotError("controller submodule revision does not match HEAD")
        return
    try:
        with os.scandir(candidate) as children:
            if next(children, None) is not None:
                raise SnapshotError(
                    "controller uninitialized submodule directory is not empty"
                )
    except OSError as error:
        raise SnapshotError("controller submodule path cannot be inspected") from error


def _verify_controller_files(repo):
    head = _head_tree(repo)
    expected_index = {
        path: (mode, object_id)
        for path, (mode, _kind, object_id) in head.items()
    }
    if _index_tree(repo) != expected_index:
        raise SnapshotError("controller index does not exactly match HEAD")
    try:
        algorithm = _git(
            ["rev-parse", "--show-object-format"], repo, safe=True
        ).decode("ascii").strip()
    except UnicodeError as error:
        raise SnapshotError("controller repository has an invalid object format") from error
    for path, (mode, kind, object_id) in head.items():
        repo_path = repo / os.fsdecode(path)
        if mode in (b"100644", b"100755") and kind == b"blob":
            actual = _regular_blob(repo_path, mode == b"100755", algorithm)
        elif mode == b"120000" and kind == b"blob":
            actual = _symlink_blob(repo_path, algorithm)
        elif mode == b"160000" and kind == b"commit":
            _verify_gitlink(repo, path, object_id)
            continue
        else:
            raise SnapshotError("controller tree contains an unsupported entry")
        if actual != object_id:
            raise SnapshotError("controller tracked file content does not match HEAD")


def _reject_ignored_governing_files(repo):
    paths = _git(
        [
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
            "--",
            *_GOVERNING_GLOBS,
        ],
        repo,
        safe=True,
        literal_paths=False,
    )
    if paths:
        raise SnapshotError("controller checkout has ignored governing files")


def _reject_untracked_files(repo):
    paths = _git(
        ["ls-files", "--others", "--exclude-standard", "-z"],
        repo,
        safe=True,
    )
    if paths:
        raise SnapshotError(
            "controller checkout is not clean; restart from a clean exact-base "
            "checkout"
        )


def _controller_at_base(repo_root, base_sha):
    requested = Path(repo_root).expanduser().resolve()
    try:
        top = Path(
            _git(["rev-parse", "--show-toplevel"], requested, safe=True)
            .decode("utf-8")
            .strip()
        ).resolve()
    except (SnapshotError, UnicodeError) as error:
        raise SnapshotError("controller root must be a Git worktree") from error
    _reject_controller_config(top)
    head = _commit(top, "HEAD")
    if head != base_sha:
        raise TargetMoved(
            "controller checkout is not at the frozen base; restart from the exact "
            "base revision"
        )
    repositories = _controller_repositories(top)
    for repo in repositories:
        _reject_index_concealment(repo)
        _reject_ignored_governing_files(repo)
        _verify_controller_files(repo)
        _reject_untracked_files(repo)
    return {"controller_root": str(top), "base_sha": head}


def _diff_command(mode):
    command = [
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--find-renames=50%",
    ]
    if mode == "names":
        command.extend(["--name-status", "-z"])
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


def _changed_paths(repo):
    fields = _git(_diff_command("names"), repo, safe=True).split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    paths = []
    changed_files = 0
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if not re.fullmatch(rb"[ACDMRTUXB][0-9]*", status):
            raise SnapshotError("local diff contains a malformed name-status record")
        path_count = 2 if status[:1] in (b"C", b"R") else 1
        if index + path_count > len(fields):
            raise SnapshotError("local diff contains a truncated name-status record")
        paths.extend(fields[index:index + path_count])
        index += path_count
        changed_files += 1
    return changed_files, paths


def _architecture_path(path):
    parts = path.split(b"/")
    return (
        len(parts) > 2
        and parts[0] == b"docs"
        and parts[1] in (b"adr", b"adrs")
        and path.lower().endswith(b".md")
    )


def _rule_path(path):
    return path.startswith(b".claude/rules/") and path.lower().endswith(b".md")


def _base_tree(repo):
    tree = _git(
        ["ls-tree", "-r", "-z", "--full-tree", "refs/underwrite/base", "--"],
        repo,
        safe=True,
    )
    entries = {}
    for record in tree.split(b"\0"):
        if not record:
            continue
        try:
            header, path = record.split(b"\t", 1)
            mode, kind, blob_sha = header.split(b" ", 2)
        except ValueError as error:
            raise SnapshotError("base tree contains a malformed entry") from error
        entries[path] = (mode, kind, blob_sha)
    return entries


def _tree_text(repo, path, entry):
    mode, kind, blob_sha = entry
    if kind != b"blob" or mode not in (b"100644", b"100755"):
        raise SnapshotError("trusted base context must contain regular files")
    try:
        size = int(
            _git(
                ["cat-file", "-s", blob_sha.decode("ascii")],
                repo,
                safe=True,
            ).decode("ascii")
        )
    except (UnicodeError, ValueError) as error:
        raise SnapshotError("trusted base context blob has an invalid size") from error
    if size > _MAX_TRUSTED_BYTES:
        raise SnapshotError("trusted base context exceeds its safety limit")
    raw = _git(
        ["cat-file", "blob", blob_sha.decode("ascii")],
        repo,
        safe=True,
    )
    if len(raw) != size:
        raise SnapshotError("trusted base context changed while reading")
    try:
        return raw, raw.decode("utf-8")
    except UnicodeError as error:
        raise SnapshotError("trusted base context must be UTF-8") from error


def _initial_context_paths(entries):
    selected = {
        path: False for path in entries if _architecture_path(path)
    }
    for path, (mode, _kind, _blob_sha) in entries.items():
        if mode in (b"120000", b"160000") and _governing_link_path(path):
            raise SnapshotError(
                "trusted base governing paths must be regular files, not symlinks or "
                "gitlinks"
            )
    if b"CONTRIBUTING.md" in entries:
        selected[b"CONTRIBUTING.md"] = False
    for path in entries:
        if path.rsplit(b"/", 1)[-1] in _GOVERNING_NAMES:
            selected[path] = True
        if _rule_path(path):
            selected[path] = True
    return selected


def _without_markdown_code(content):
    visible = []
    fence = None
    for line in content.splitlines(keepends=True):
        marker = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})", line)
        if fence is not None:
            if marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= fence[1]:
                fence = None
            continue
        if marker:
            fence = (marker.group(1)[0], len(marker.group(1)))
            continue
        if line.startswith(("    ", "\t")):
            continue
        output = []
        index = 0
        while index < len(line):
            if line[index] != "`":
                output.append(line[index])
                index += 1
                continue
            end = index
            while end < len(line) and line[end] == "`":
                end += 1
            marker_text = line[index:end]
            close = line.find(marker_text, end)
            if close < 0:
                output.append(marker_text)
                index = end
            else:
                index = close + len(marker_text)
        visible.append("".join(output))
    return re.sub(r"<!--.*?-->", "", "".join(visible), flags=re.DOTALL)


def _imported_paths(path, content):
    parent = posixpath.dirname(path)
    for match in _IMPORT.finditer(_without_markdown_code(content)):
        reference = match.group("path")
        if reference.startswith(("/", "~")) or "://" in reference:
            raise SnapshotError("trusted base instruction import must stay in the repo")
        candidate = posixpath.normpath(posixpath.join(parent, reference))
        if candidate in (".", "..") or candidate.startswith("../"):
            raise SnapshotError("trusted base instruction import escapes the repo")
        yield candidate.encode("utf-8")


def _trusted_context(repo, base_sha, destination):
    entries = _base_tree(repo)
    initial = _initial_context_paths(entries)
    pending = [
        (path, 0, (path,), follows_imports)
        for path, follows_imports in initial.items()
    ]
    loaded = {}
    total_bytes = 0
    while pending:
        path, depth, chain, follows_imports = pending.pop()
        if path in loaded:
            continue
        entry = entries.get(path)
        if entry is None:
            raise SnapshotError("trusted base instruction import is missing")
        mode, _kind, blob_sha = entry
        try:
            text_path = path.decode("utf-8")
            raw, content = _tree_text(repo, path, entry)
        except UnicodeError as error:
            raise SnapshotError("trusted base context must be UTF-8") from error
        total_bytes += len(raw)
        if len(loaded) + 1 > _MAX_TRUSTED_FILES or total_bytes > _MAX_TRUSTED_BYTES:
            raise SnapshotError("trusted base context exceeds its safety limit")
        loaded[path] = {
            "blob_sha": blob_sha.decode("ascii"),
            "content": content,
            "mode": mode.decode("ascii"),
            "path": text_path,
        }
        if follows_imports:
            imports = list(_imported_paths(text_path, content))
            if imports and depth >= _MAX_IMPORT_DEPTH:
                raise SnapshotError("trusted base instruction import exceeds max depth")
            for imported in imports:
                if imported in chain:
                    raise SnapshotError("trusted base instruction import contains a cycle")
                pending.append((imported, depth + 1, chain + (imported,), True))
    files = sorted(loaded.values(), key=lambda entry: entry["path"])
    document = {"base_sha": base_sha, "files": files, "version": 1}
    destination.write_text(
        json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    with destination.open("rb") as handle:
        os.fsync(handle.fileno())
    return destination


def _object_bundle(repo, destination):
    _git(
        [
            "bundle",
            "create",
            str(destination),
            "refs/underwrite/base",
            "refs/underwrite/head",
        ],
        repo,
        safe=True,
    )
    with destination.open("rb") as handle:
        os.fsync(handle.fileno())
    return destination


@contextlib.contextmanager
def _review_repo(store):
    with tempfile.TemporaryDirectory(prefix="underwrite-objects-") as temporary:
        temporary = Path(temporary)
        bundle = temporary / "pr.bundle"
        target = store.copy_verified_object_bundle(bundle)
        bare = temporary / "review.git"
        _run(
            ["git", "init", "--bare", "--template=", str(bare)],
            env=_git_environment(),
        )
        _git(
            [
                "fetch",
                "--atomic",
                "--no-tags",
                "--recurse-submodules=no",
                str(bundle),
                "+refs/underwrite/base:refs/underwrite/base",
                "+refs/underwrite/head:refs/underwrite/head",
            ],
            bare,
            safe=True,
        )
        if (
            _commit(bare, "refs/underwrite/base") != target["base_sha"]
            or _commit(bare, "refs/underwrite/head") != target["head_sha"]
        ):
            raise Conflict("frozen PR object bundle refs do not match the target")
        yield bare, target


def _tree_entries(repo, commit):
    raw = _git(
        ["ls-tree", "-r", "-z", "--full-tree", commit, "--"],
        repo,
        safe=True,
    )
    entries = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            header, path = record.split(b"\t", 1)
            mode, kind, object_sha = header.split(b" ", 2)
        except ValueError as error:
            raise SnapshotError("frozen PR tree contains a malformed entry") from error
        entries[path] = (mode, kind, object_sha)
    return entries


def read_blob(store, side, path, max_bytes):
    if side not in ("base", "head"):
        raise SnapshotError("blob side must be base or head")
    if not isinstance(path, str) or not path or "\0" in path:
        raise SnapshotError("blob path must be non-empty UTF-8 text")
    try:
        path_bytes = path.encode("utf-8")
    except UnicodeError as error:
        raise SnapshotError("blob path must be UTF-8") from error
    if (
        path.startswith("/")
        or any(part in ("", ".", "..") for part in path.split("/"))
    ):
        raise SnapshotError("blob path must be repo-relative")
    _positive(max_bytes, "blob max_bytes")
    if max_bytes > _MAX_BLOB_BYTES:
        raise SnapshotError(f"blob max_bytes cannot exceed {_MAX_BLOB_BYTES}")
    with _review_repo(store) as (repo, target):
        commit = target[f"{side}_sha"]
        entry = _tree_entries(repo, commit).get(path_bytes)
        if entry is None:
            raise SnapshotError(f"{side} has no path {path!r}")
        mode, kind, object_sha = entry
        if kind != b"blob":
            raise SnapshotError(f"{side} path {path!r} is not a blob")
        try:
            size = int(
                _git(
                    ["cat-file", "-s", object_sha.decode("ascii")],
                    repo,
                    safe=True,
                ).decode("ascii")
            )
        except (UnicodeError, ValueError) as error:
            raise SnapshotError("frozen PR blob has an invalid size") from error
        if size > max_bytes:
            raise SnapshotError(
                f"{side} blob {path!r} is {size} bytes, above max_bytes {max_bytes}"
            )
        data = _git(
            ["cat-file", "blob", object_sha.decode("ascii")],
            repo,
            safe=True,
        )
        if len(data) != size:
            raise SnapshotError("frozen PR blob size changed while reading")
    try:
        content = data.decode("utf-8")
        encoding = "utf-8"
    except UnicodeError:
        content = base64.b64encode(data).decode("ascii")
        encoding = "base64"
    return {
        "blob_sha": object_sha.decode("ascii"),
        "bytes": size,
        "content": content,
        "encoding": encoding,
        "mode": mode.decode("ascii"),
        "path": path,
        "side": side,
    }


def context_log(store, limit):
    _positive(limit, "context log limit")
    if limit > _MAX_CONTEXT_COMMITS:
        raise SnapshotError(
            f"context log limit cannot exceed {_MAX_CONTEXT_COMMITS}"
        )
    with _review_repo(store) as (repo, target):
        _count, raw_paths = _changed_paths(repo)
        try:
            paths = [path.decode("utf-8") for path in raw_paths]
        except UnicodeError as error:
            raise SnapshotError("changed paths must be UTF-8 for context log") from error
        if not paths:
            return {"base_sha": target["base_sha"], "commits": [], "paths": []}
        raw = _git(
            [
                "log",
                f"--max-count={limit}",
                "-z",
                "--format=%H%x00%P%x00%an%x00%aI%x00%s",
                "refs/underwrite/base",
                "--",
                *paths,
            ],
            repo,
            safe=True,
        )
    fields = raw.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if len(fields) % 5:
        raise SnapshotError("frozen base log has a malformed record")
    commits = []
    for index in range(0, len(fields), 5):
        commit, parents, author, authored_at, subject = fields[index:index + 5]
        try:
            commit_id = commit.decode("ascii")
            parent_ids = parents.decode("ascii").split()
        except UnicodeError as error:
            raise SnapshotError("frozen base log has a non-ASCII object ID") from error
        if not _FULL_SHA.fullmatch(commit_id):
            raise SnapshotError("frozen base log has an invalid commit ID")
        if not all(_FULL_SHA.fullmatch(parent) for parent in parent_ids):
            raise SnapshotError("frozen base log has an invalid parent ID")
        commits.append({
            "author": author.decode("utf-8", "replace"),
            "authored_at": authored_at.decode("utf-8", "replace"),
            "parents": parent_ids,
            "sha": commit_id,
            "subject": subject.decode("utf-8", "replace"),
        })
    return {"base_sha": target["base_sha"], "commits": commits, "paths": paths}


def capture(store, repo, number, controller_root, api=load_pr):
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
    _controller_at_base(controller_root, identity["base_sha"])

    clone_url = _field(first, ("base", "repo", "clone_url"), str)
    changed_files = _field(first, ("changed_files",))
    if isinstance(changed_files, bool) or not isinstance(changed_files, int) or changed_files < 0:
        raise SnapshotError("PR metadata changed_files must be a non-negative integer")

    with tempfile.TemporaryDirectory(prefix="underwrite-pr-") as temporary:
        temporary = Path(temporary)
        bare = temporary / "review.git"
        _run(
            ["git", "init", "--bare", "--template=", str(bare)],
            env=_git_environment(),
        )
        _fetch_pr_objects(bare, clone_url, identity, number)

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

        local_files, touched_paths = _changed_paths(bare)
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

        trusted_context = _trusted_context(
            bare,
            identity["base_sha"],
            temporary / "trusted-context.json",
        )
        object_bundle = _object_bundle(bare, temporary / "pr.bundle")

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
        _controller_at_base(controller_root, identity["base_sha"])
        return store.freeze_target(
            target,
            diff,
            metadata,
            trusted_context,
            object_bundle,
        )


def check_controller(store, repo_root):
    target = store.verify_target_files()
    return _controller_at_base(repo_root, target["base_sha"])


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
    store.check_execution()
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
    store.check_execution()
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
