"""Which lines a unified diff touches, and where a cited location lands in it.

The anchor validator, the renderer and the store all answer the same question of the
frozen diff, so they share one parser rather than three readings of a hunk header.
"""

import re

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# `path:line`, with an optional range tail the page tolerates.
LOCATION_LINE = re.compile(r"^(?P<path>.+?):(?P<line>\d+)(?:[-,]\d+)*$")
SNAP_WINDOW = 20


def clean_path(raw):
    p = raw.split("\t")[0].strip()
    if p.startswith('"') and p.endswith('"'):
        # Git quotes a non-ASCII path as octal escapes of its UTF-8 bytes, so the
        # escapes have to become bytes again before decoding. Going straight from
        # unicode_escape to str reads each byte as a codepoint: café becomes cafÃ©.
        try:
            p = p[1:-1].encode().decode("unicode_escape").encode("latin-1").decode("utf-8")
        except UnicodeError:
            p = p[1:-1]  # not UTF-8, so anchor on the raw name rather than give up
    return p[2:] if p[:2] in ("a/", "b/") else p


def parse_diff(text):
    """Return {path: {"LEFT": {old lines}, "RIGHT": {new lines}, "hunks": [hunk]}}.

    A hunk is {"LEFT": set, "RIGHT": set, "pos": {(side, line): index}}. The flat sets
    answer "is this line in the diff"; the hunks answer the two questions GitHub asks
    of a multi-line comment and rejects the whole review over, which a flat set cannot:
    are both ends in the same hunk, and does the start really come first.
    """
    files, path, was = {}, None, None
    hunk = None
    old_ln = new_ln = rem_old = rem_new = 0

    # Only \n ends a diff line. splitlines() also breaks on \r, \v, \f, \x1c-\x1e,
    # \x85 and U+2028-9, any of which shatters a line whose content carries one.
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    for line in lines:
        line = line.rstrip("\r")
        # Inside a hunk, consume by the header's line counts. Counting rather than
        # pattern-matching keeps an added line of "++ x" from looking like a header.
        if rem_old > 0 or rem_new > 0:
            if line.startswith("\\"):
                continue
            tag = line[:1]
            if tag == " " or line == "":
                mark(files[path], hunk, "LEFT", old_ln)
                mark(files[path], hunk, "RIGHT", new_ln)
                old_ln, new_ln, rem_old, rem_new = old_ln + 1, new_ln + 1, rem_old - 1, rem_new - 1
            elif tag == "+":
                mark(files[path], hunk, "RIGHT", new_ln)
                new_ln, rem_new = new_ln + 1, rem_new - 1
            elif tag == "-":
                mark(files[path], hunk, "LEFT", old_ln)
                old_ln, rem_old = old_ln + 1, rem_old - 1
            else:
                rem_old = rem_new = 0  # malformed hunk, resync on the next header
            continue

        m = HUNK.match(line)
        if m and path:
            old_ln, new_ln = int(m.group(1)), int(m.group(3))
            rem_old = int(m.group(2) or 1)
            rem_new = int(m.group(4) or 1)
            hunk = {"LEFT": set(), "RIGHT": set(), "pos": {}}
            files[path]["hunks"].append(hunk)
        elif line.startswith("diff --git "):
            path = was = None  # so a later /dev/null cannot inherit a stale path
        elif line.startswith("--- "):
            target = line[4:].strip()
            was = None if target == "/dev/null" else clean_path(target)
        elif line.startswith("+++ "):
            target = line[4:].strip()
            # A deleted file has no new path, and GitHub anchors its comments on the
            # old one. Skipping it left deletions, the tier walked last precisely
            # because it carries the most risk, with nowhere to hang a comment.
            path = was if target == "/dev/null" else clean_path(target)
            if path:
                files.setdefault(path, {"LEFT": set(), "RIGHT": set(), "hunks": []})

    return files


def mark(entry, hunk, side, line):
    """Record a line on both the file-wide set and the hunk it came from."""
    entry[side].add(line)
    if hunk is not None:
        hunk[side].add(line)
        hunk["pos"].setdefault((side, line), len(hunk["pos"]))


def hunk_holding(sides, side, line):
    for hunk in sides.get("hunks") or ():
        if line in hunk[side]:
            return hunk
    return None


def snap(line, valid):
    """Nearest valid line within SNAP_WINDOW, preferring the earlier one on a tie."""
    if not valid:
        return None
    best = min(valid, key=lambda v: (abs(v - line), v))
    return best if abs(best - line) <= SNAP_WINDOW else None


def resolve_path(candidate, paths):
    """Resolve a cited location against the frozen diff, exactly or by unique suffix.

    A beat cites `scopes.test.ts` for a path three directories deep, so the suffix leg
    earns its keep; an ambiguous suffix resolves to nothing rather than to a guess.
    """
    if not candidate or candidate in paths:
        return candidate or None
    tail = "/" + candidate
    found = [path for path in paths if path.endswith(tail)]
    return found[0] if len(found) == 1 else None


def untouched_anchor(where, files):
    """Why a flag's `where` does not land inside the diff, or None when it does.

    The first `path:line` is the anchor, tried on the whole string first so a path
    with a space in it is not shattered. The number reads as the head does, or as the
    base did for a line the PR removed; context lines count. The question is whether
    the PR put this line in front of the reviewer, not whether it added it.
    """
    text = str(where or "")
    cited = LOCATION_LINE.match(text.strip().rstrip(",;+·"))
    for token in () if cited else text.split(" "):
        cited = LOCATION_LINE.match(token.rstrip(",;+·"))
        if cited:
            break
    if not cited:
        return "flag needs a path:line anchor inside the frozen diff"
    candidate, line = cited["path"], int(cited["line"])
    path = resolve_path(candidate, sorted(files))
    if path is None:
        return f"flag names {candidate}, which the frozen diff does not touch"
    touched = files[path]["LEFT"] | files[path]["RIGHT"]
    if line in touched:
        return None
    if not touched:
        return f"flag names {path}, which the frozen diff touches with no lines"
    nearest = min(touched, key=lambda v: (abs(v - line), v))
    return (
        f"flag at {path}:{line} is outside the frozen diff; "
        f"nearest touched line is {path}:{nearest}"
    )
