"""Which lines a unified diff touches, and where a cited location lands in it.

The anchor validator, the renderer and the store all answer the same question of the
frozen diff, so they share one parser rather than three readings of a hunk header.
"""

import re

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# `path:line`, with an optional range tail the page tolerates.
LOCATION_LINE = re.compile(r"^(?P<path>.+?):(?P<line>\d+)(?:[-,]\d+)*$")
SNAP_WINDOW = 20


_GIT_ESCAPES = {
    "a": 0x07,
    "b": 0x08,
    "t": 0x09,
    "n": 0x0A,
    "v": 0x0B,
    "f": 0x0C,
    "r": 0x0D,
    '"': 0x22,
    "\\": 0x5C,
}


def unquote_git_path(raw, strict_utf8=False):
    text = raw.strip()
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        inner = text[1:-1]
        raw_bytes = bytearray()
        index = 0
        while index < len(inner):
            ch = inner[index]
            if ch != "\\":
                raw_bytes.extend(ch.encode("utf-8"))
                index += 1
                continue
            index += 1
            if index >= len(inner):
                raw_bytes.append(0x5C)
                break
            esc = inner[index]
            if "0" <= esc <= "7":
                octal = esc
                index += 1
                for _ in range(2):
                    if index < len(inner) and "0" <= inner[index] <= "7":
                        octal += inner[index]
                        index += 1
                    else:
                        break
                raw_bytes.append(int(octal, 8) & 0xFF)
            elif esc in _GIT_ESCAPES:
                raw_bytes.append(_GIT_ESCAPES[esc])
                index += 1
            else:
                raw_bytes.extend(esc.encode("utf-8"))
                index += 1
        try:
            return raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            if strict_utf8:
                return None
            return inner
    return text


def clean_path(raw):
    p = raw.split("\t")[0].strip()
    if p.startswith('"') and p.endswith('"'):
        # Git quotes a non-ASCII path as octal escapes of its UTF-8 bytes, so the
        # escapes have to become bytes again before decoding.
        unquoted = unquote_git_path(p)
        p = p[1:-1] if unquoted is None else unquoted
    return p[2:] if p[:2] in ("a/", "b/") else p


def _consume_quoted_token(text):
    if not text.startswith('"'):
        return None, text
    escaped = False
    for index in range(1, len(text)):
        ch = text[index]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            return text[:index + 1], text[index + 1:]
    return None, text


def _strip_side_prefix(path, side):
    if path is None:
        return None
    prefix = f"{side}/"
    return path[len(prefix):] if path.startswith(prefix) else None


def _split_diff_git_header(rest):
    if not rest:
        return ()
    if rest.startswith('"'):
        first_token, tail = _consume_quoted_token(rest)
        if first_token is None or not tail.startswith(" "):
            return ()
        second_part = tail[1:]
        if second_part.startswith('"'):
            second_token, extra = _consume_quoted_token(second_part)
            if second_token is None or extra:
                return ()
            left = _strip_side_prefix(unquote_git_path(first_token, strict_utf8=True), "a")
            right = _strip_side_prefix(unquote_git_path(second_token, strict_utf8=True), "b")
        else:
            left = _strip_side_prefix(unquote_git_path(first_token, strict_utf8=True), "a")
            right = _strip_side_prefix(second_part, "b")
        return tuple(p for p in (left, right) if p)
    if not rest.startswith("a/"):
        return ()
    body = rest[2:]
    quote_sep = body.find(' "')
    if quote_sep != -1:
        second_token, extra = _consume_quoted_token(body[quote_sep + 1:])
        if second_token is not None and not extra:
            left = body[:quote_sep]
            right = _strip_side_prefix(unquote_git_path(second_token, strict_utf8=True), "b")
            return tuple(p for p in (left, right) if p)
    candidates = []
    start = 0
    while True:
        marker = body.find(" b/", start)
        if marker == -1:
            break
        left, right = body[:marker], body[marker + 3:]
        if left and right:
            candidates.append((left, right))
        start = marker + 1
    for left, right in candidates:
        if left == right:
            return (left,)
    if len(candidates) == 1:
        return candidates[0]
    return candidates or ()


def diff_paths(data):
    """Every UTF-8 path touched in a unified Git diff, including binary and mode-only headers."""
    paths = set()
    pending_candidates = ()
    saw_explicit_path = False

    def flush():
        nonlocal pending_candidates, saw_explicit_path
        if pending_candidates and not saw_explicit_path:
            if isinstance(pending_candidates, list):
                for left, right in pending_candidates:
                    paths.add(left)
                    paths.add(right)
            else:
                paths.update(pending_candidates)
        pending_candidates = ()
        saw_explicit_path = False

    def add_decoded(raw, strip_side=False):
        value = unquote_git_path(raw, strict_utf8=True)
        if value is not None:
            if strip_side and value[:2] in ("a/", "b/"):
                value = value[2:]
            if value:
                paths.add(value)

    lines = data.split(b"\n") if isinstance(data, (bytes, bytearray)) else data.encode("utf-8").split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    in_hunk = False
    rem_old = rem_new = 0
    for raw_line in lines:
        raw_line = raw_line.rstrip(b"\r")
        if in_hunk and (rem_old > 0 or rem_new > 0):
            if raw_line.startswith(b"\\"):
                continue
            tag = raw_line[:1]
            if tag == b" " or raw_line == b"":
                rem_old -= 1
                rem_new -= 1
            elif tag == b"+":
                rem_new -= 1
            elif tag == b"-":
                rem_old -= 1
            else:
                rem_old = rem_new = 0
                in_hunk = False
            if rem_old <= 0 and rem_new <= 0:
                in_hunk = False
            continue
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError:
            continue
        m = HUNK.match(line)
        if m:
            rem_old = int(m.group(2) or 1)
            rem_new = int(m.group(4) or 1)
            in_hunk = rem_old > 0 or rem_new > 0
            continue
        if line.startswith("diff --git "):
            flush()
            pending_candidates = _split_diff_git_header(line[len("diff --git "):])
        elif line.startswith(("rename from ", "copy from ")):
            saw_explicit_path = True
            prefix = "rename from " if line.startswith("rename from ") else "copy from "
            add_decoded(line[len(prefix):])
        elif line.startswith(("rename to ", "copy to ")):
            saw_explicit_path = True
            prefix = "rename to " if line.startswith("rename to ") else "copy to "
            add_decoded(line[len(prefix):])
        elif line.startswith(("--- ", "+++ ")):
            target = line[4:].split("\t")[0].strip()
            if target != "/dev/null":
                saw_explicit_path = True
                add_decoded(target, strip_side=True)
    flush()
    return sorted(paths)


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
                mark(files[path], hunk, "LEFT", old_ln, line[1:])
                mark(files[path], hunk, "RIGHT", new_ln, line[1:])
                old_ln, new_ln, rem_old, rem_new = old_ln + 1, new_ln + 1, rem_old - 1, rem_new - 1
            elif tag == "+":
                mark(files[path], hunk, "RIGHT", new_ln, line[1:])
                new_ln, rem_new = new_ln + 1, rem_new - 1
            elif tag == "-":
                mark(files[path], hunk, "LEFT", old_ln, line[1:])
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
                files.setdefault(path, {"LEFT": set(), "RIGHT": set(), "hunks": [], "text": {}})

    return files


def mark(entry, hunk, side, line, text=""):
    """Record a line on both the file-wide set and the hunk it came from."""
    entry[side].add(line)
    entry["text"][(side, line)] = text
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

    Every `path:line` in the prose is a citation, and one that lands is enough. The
    whole string is tried first so a path with a space in it is not shattered, but only
    counts when it resolves to a diff path; otherwise the citations are read token by
    token, the way the page links them. The number reads as the head does, or as the
    base did for a line the PR removed; context lines count. The question is whether
    the PR put this line in front of the reviewer, not whether it added it.
    """
    paths = sorted(files)
    text = str(where or "").strip()
    whole = LOCATION_LINE.match(text.rstrip(",;+\u00b7"))
    if whole and resolve_path(whole["path"], paths):
        citations = [whole]
    else:
        citations = [
            cited
            for cited in (LOCATION_LINE.match(t.rstrip(",;+\u00b7")) for t in text.split(" "))
            if cited
        ]
    if not citations:
        return "flag needs a path:line anchor inside the frozen diff"
    for cited in citations:
        path = resolve_path(cited["path"], paths)
        if path is not None and int(cited["line"]) in files[path]["LEFT"] | files[path]["RIGHT"]:
            return None
    candidate, line = citations[0]["path"], int(citations[0]["line"])
    path = resolve_path(candidate, paths)
    if path is None:
        return f"flag names {candidate}, which the frozen diff does not touch"
    touched = files[path]["LEFT"] | files[path]["RIGHT"]
    if not touched:
        return f"flag names {path}, which the frozen diff touches with no lines"
    nearest = min(touched, key=lambda v: (abs(v - line), v))
    return (
        f"flag at {path}:{line} is outside the frozen diff; "
        f"nearest touched line is {path}:{nearest}"
    )


def anchor_line(where, files):
    """The first cited line that lands in the diff, as {path, text}, head side first.

    A line number moves when code above it does, so a finding is matched across heads by
    the words on its line rather than by where they sit. Words that appear on more than one
    line of the file's diff identify nothing, so they yield None.
    """
    paths = sorted(files)
    text = str(where or "").strip()
    whole = LOCATION_LINE.match(text.rstrip(",;+\u00b7"))
    tokens = [whole] if whole and resolve_path(whole["path"], paths) else [
        LOCATION_LINE.match(t.rstrip(",;+\u00b7")) for t in text.split(" ")
    ]
    for cited in tokens:
        path = resolve_path(cited["path"], paths) if cited else None
        if path is None:
            continue
        for side in ("RIGHT", "LEFT"):
            words = files[path]["text"].get((side, int(cited["line"])))
            if words is None or not words.strip():
                continue
            # Indentation can carry meaning, and words repeated in the diff name no one line.
            words = words.rstrip()
            twins = sum(1 for (s, _n), t in files[path]["text"].items() if s == side and t.rstrip() == words)
            return {"path": path, "text": words} if twins == 1 else None
    return None
