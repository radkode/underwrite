#!/usr/bin/env python3
"""
Render an underwrite session into a self-contained report page.

Reads <session-dir>/session.json and <session-dir>/beats/*.json, inlines
assets/report.css, and writes one HTML file that makes no external requests.

Output is a body fragment, which is what the Artifact tool wants. Pass
--standalone for a document shell when the page will be opened as a local file.

Beats are ordered by what is owed, not by beat number: open flags first, then
accepted, then walked-and-clean. That ordering is the whole point of the page.

Exit 0  every beat validated
Exit 2  the page rendered, but some beat failed validation and carries an
        UNPROVEN chip. Never fail at the last step of a session.
Exit 1  usage or parse error
"""
import argparse
import html
import json
import re
import sys
from pathlib import Path

SLOTS = ("what", "why", "proof", "risk", "prior", "fix")
STATES = ("clean", "flag", "unverified", "accepted", "dropped", "decided")

# state -> (css suffix, token shown before the claim)
# `decided` borrows the accepted palette on purpose: the reviewer said yes to both, and
# they sit in the same section, so the token is what separates them.
STATE_STYLE = {
    "clean": ("clean", "CLEAN"),
    "flag": ("flag", "FLAG"),
    "unverified": ("unver", "UNVERIFIED"),
    "accepted": ("acc", "ACCEPTED"),
    "dropped": ("drop", "DROPPED"),
    "decided": ("acc", "DECIDED"),
}

# (heading, hint, states, expanded by default)
SECTIONS = (
    ("Needs your call", "out of beat order, on purpose", ("flag",), True),
    ("Accepted", "your call, and what came of it", ("accepted", "decided"), True),
    ("Walked and clean", "nothing owed, proof on each", ("clean", "unverified"), False),
    ("Dropped", "raised, then set aside", ("dropped",), False),
)

LANDS_TAG = {"landed": "Landed", "ready": "Ready", "open": "Your call"}

# proof has to point at something a reader can re-run or open, or say up front
# that it does not. "inferred" is the documented honest answer, not a failure.
# The path arm wants a letter rather than a dot: requiring an extension turned
# Makefile:12 and CODEOWNERS:8 into unproven, and a letter still keeps 10:30 out.
PROOF_EVIDENCE = re.compile(r"`[^`]+`|\b[\w./-]*[A-Za-z][\w./-]*:\d+|^inferred\b")

# Only for --standalone. The viewport tag is load-bearing: report.css has a
# 620px breakpoint that never fires without it.
SHELL = (
    '<!doctype html>\n<html lang="en">\n<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
)

# Only for --live, which serve.py uses. Polls a fingerprint and swaps the body when
# it moves, preserving which beats the reviewer had expanded. Decisions POST back.
LIVE_JS = """<script>
(() => {
  const live = document.getElementById('live');
  const body = document.getElementById('live-body');
  let rev = null, known = new Set(), usable = false, sending = false;

  const beatIds = () => new Set([...document.querySelectorAll('.beat')].map(d => d.dataset.n));
  const openIds = () => new Set([...document.querySelectorAll('.beat[open]')].map(d => d.dataset.n));

  // Acting mid-action races the walk, so the controls close while one is running. They
  // stay open when no walk is listening: the call is appended either way, and a page
  // that goes dead the moment nobody is home is how a session looks broken when it is
  // only unattended.
  function applyState(state) {
    const status = state.status || {};
    const phase = status.phase || 'working';
    const listening = state.listening !== false;
    usable = listening ? phase === 'parked' : true;
    live.className = 'live ' + (listening ? phase : 'away');
    live.textContent = listening
      ? (status.text || phase)
      : 'no walk is listening, your call is saved for whenever one returns';
    if (!sending) enable(usable);
  }

  const enable = on => document.querySelectorAll('.act, .note')
    .forEach(el => el.disabled = !on);

  async function swap() {
    const open = openIds();
    body.innerHTML = await (await fetch('./fragment')).text();
    document.querySelectorAll('.beat').forEach(d => {
      if (open.has(d.dataset.n)) d.open = true;
      if (!known.has(d.dataset.n)) d.classList.add('is-new');
    });
    known = beatIds();
    wire();
    enable(usable);
  }

  async function act(button) {
    const row = button.closest('.acts');
    const n = row.dataset.acts;
    const note = row.querySelector('.note');
    const msg = row.querySelector('.act-msg');
    sending = true;
    enable(false);
    msg.textContent = 'sending';
    try {
      const sent = await fetch('./act', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          n: n === 'walk' ? null : +n,
          action: button.dataset.action,
          note: note ? note.value.trim() : '',
        }),
      });
      if (!sent.ok) throw new Error((await sent.text()).trim() || sent.status);
      msg.textContent = '';
    } catch (err) {
      msg.textContent = 'not sent, ' + err.message;
      enable(true);
    }
    sending = false;
  }

  const wire = () => document.querySelectorAll('.act').forEach(b => b.onclick = () => act(b));

  const stream = new EventSource('./events');
  stream.onmessage = event => {
    const state = JSON.parse(event.data);
    applyState(state);
    if (rev !== null && state.rev !== rev) swap();
    rev = state.rev;
  };
  stream.onerror = () => {
    live.className = 'live down';
    live.textContent = 'reconnecting';
    enable(false);
  };

  known = beatIds();
  wire();
  enable(false);
})();
</script>"""


def attr(value):
    """Escape for attribute position, quotes included. `md` deliberately leaves them
    alone so its `<code>` output reads right in a text node, which makes it exactly
    the wrong helper here."""
    return html.escape(str(value), quote=True)


def md(text):
    """Escape, then let backticks become <code>. No HTML passthrough."""
    out = html.escape(str(text), quote=False)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", out)


def validate(beat, mode="branch", final=False):
    """Return a list of problems. Empty means the beat is shippable."""
    problems = []
    n = beat.get("n", "?")
    state = beat.get("state")
    slots = beat.get("slots") or {}

    if state not in STATES:
        problems.append(f"beat {n}: state {state!r} is not one of {', '.join(STATES)}")
    for key in slots:
        if key not in SLOTS:
            problems.append(f"beat {n}: unknown slot {key!r}")
    if not slots.get("what"):
        problems.append(f"beat {n}: no what")
    if state in ("clean", "accepted") and not slots.get("proof"):
        problems.append(f"beat {n}: {state} with no proof")
    # Review mode renders once before the POST. Its delivered report has the same
    # obligation as branch mode, with the review URL in place of a commit.
    if (mode == "branch" or final) and state == "accepted" and not beat.get("landed"):
        problems.append(f"beat {n}: accepted, nothing landed")
    # The escape from that rule, for the flag whose answer is a call rather than a patch.
    # Then the words are the whole artifact, and a decided beat with none of them is the
    # same silence the rule above exists to catch.
    if state == "decided" and not beat.get("call"):
        problems.append(f"beat {n}: decided, nothing recorded")
    # The mirror, and the shape a decision taken in words leaves when it never reaches
    # the server: the fix committed, the beat still open, and the rule above looking
    # straight past it because it keys on the state that was never set.
    if beat.get("landed") and state != "accepted":
        problems.append(f"beat {n}: landed {beat['landed']} but state is {state!r}")
    if state == "flag":
        for key in ("risk", "fix"):
            if not slots.get(key):
                problems.append(f"beat {n}: flag with no {key}")
    proof = slots.get("proof")
    if proof and not isinstance(proof, str):
        # Reported rather than raised: searching a number threw TypeError straight
        # past main(), so the one step that promises never to fail did.
        problems.append(f"beat {n}: proof is {type(proof).__name__}, not text")
    elif proof and not PROOF_EVIDENCE.search(proof):
        problems.append(f"beat {n}: proof names no command or path:line")
    return problems


def diff_html(lines):
    out = []
    for line in lines:
        head = line[:1]
        kind = {"+": "add", "-": "del", " ": "ctx"}.get(head, "file")
        out.append(f'<span class="l {kind}">{html.escape(line, quote=False)}</span>')
    return f'<div class="diff"><pre>{"".join(out)}</pre></div>'


def beat_html(beat, problems, expanded, live=False):
    suffix, token = STATE_STYLE.get(beat.get("state"), ("unver", "UNVERIFIED"))
    slots = beat.get("slots") or {}
    n = beat.get("n", "?")

    chip = '<span class="unproven">unproven</span>' if problems else ""
    rows = []
    for key in SLOTS:
        value = slots.get(key)
        if not value:
            continue
        cls = ' class="risk"' if key == "risk" else ' class="fix"' if key == "fix" else ""
        rows.append(f"<dt{cls}>{key}</dt><dd{cls}>{md(value)}</dd>")

    body = [f'<dl class="slots">{"".join(rows)}</dl>']
    if beat.get("diff"):
        body.append(diff_html(beat["diff"]))
    if beat.get("call"):
        body.append(
            '<div class="call"><span class="lbl">Your call · beat '
            f'{n}</span><q>{md(beat["call"])}</q></div>'
        )
    if beat.get("landed"):
        branch = beat.get("branch")
        body.append(
            '<div class="shipped"><span class="lbl">Landed</span>'
            f'<code>{md(beat["landed"])}</code>'
            + (f"<span>on</span><code>{md(branch)}</code>" if branch else "")
            + "</div>"
        )
    if live:
        flag = beat.get("state") == "flag"
        controls = (
            '<button class="act primary" data-action="accept">Accept</button>'
            '<button class="act" data-action="drop">Drop</button>'
            if flag
            else '<button class="act" data-action="note">Save note</button>'
        )
        placeholder = "or put it in your own words" if flag else "note this for the record"
        body.append(
            f'<div class="acts" data-acts="{attr(n)}">{controls}'
            f'<input class="note" aria-label="your words, beat {attr(n)}" placeholder="{placeholder}">'
            f'<span class="act-msg"></span></div>'
        )

    return (
        f'<details class="beat s-{suffix}" data-n="{attr(n)}"{" open" if expanded else ""}>'
        f"<summary>"
        f'<span class="b-num">{md(n)}</span>'
        f'<span class="b-tier">{md(beat.get("tier", ""))}</span>'
        f'<span class="b-claim"><span class="state">{token}</span> &nbsp;'
        f'{md(beat.get("claim", ""))}{chip}</span>'
        f'<span class="b-path">{md(beat.get("where", ""))}</span>'
        f"</summary>"
        f'<div class="b-body">{"".join(body)}</div>'
        f"</details>"
    )


def body_html(session, beats, problems_by_n, live=False):
    """Everything below the masthead. This is what /fragment re-serves on a change."""
    counts = {}
    for beat in beats:
        counts[beat.get("state")] = counts.get(beat.get("state"), 0) + 1

    tiles = [
        ("is-clean", counts.get("clean", 0) + counts.get("unverified", 0), "clean"),
        ("is-flag", counts.get("flag", 0), "needs your call"),
        ("is-acc", counts.get("accepted", 0), "accepted"),
        ("is-mute", len(beats), "beats walked"),
    ]
    parts = [
        '<div class="counts">'
        + "".join(
            f'<div class="count {cls}"><span class="n">{n}</span>'
            f'<span class="k">{k}</span></div>'
            for cls, n, k in tiles
        )
        + "</div>"
    ]

    # A state outside the five matches no section, and a beat that matches no section
    # used to render nowhere while still counting in the tiles. The page exists to say
    # what is owed, so an unplaceable beat is shown, not dropped.
    placed = {state for _h, _t, states, _e in SECTIONS for state in states}
    sections = (*SECTIONS, ("Unplaced", "state is not one of the five", None, True))

    for heading, hint, states, expanded in sections:
        if states is None:
            picked = [b for b in beats if b.get("state") not in placed]
        else:
            picked = [b for b in beats if b.get("state") in states]
        if not picked:
            continue
        cards = "".join(
            beat_html(b, problems_by_n.get(b.get("n")), expanded, live) for b in picked
        )
        parts.append(
            f'<section class="sec"><div class="sec-head"><h2>{heading}</h2>'
            f'<span class="hint">{hint}</span></div>{cards}</section>'
        )

    if session.get("lands"):
        rows = "".join(
            f'<div class="next-row {attr(l.get("state", "open"))}">'
            f'<span class="tag">{LANDS_TAG.get(l.get("state"), "Your call")}</span>'
            f'<span class="what">{md(l.get("what", ""))}</span>'
            f'<span class="where">{md(l.get("where", ""))}</span></div>'
            for l in session["lands"]
        )
        audience = session.get("audience")
        hint = audience.get("why", "") if isinstance(audience, dict) else ""
        parts.append(
            '<section class="sec"><div class="sec-head"><h2>What lands</h2>'
            f'<span class="hint">{md(hint)}</span></div>'
            f'<div class="next">{rows}</div></section>'
        )
    return "\n".join(parts)


def render(session, beats, css, problems_by_n, live=False):
    number = session.get("number")
    label = f"#{number}" if number else session.get("head", "")[:7]

    parts = [
        f'<title>{html.escape(label)} underwrite · {html.escape(session.get("repo", ""))}</title>',
        f"<style>\n{css}\n</style>",
        '<div class="page">',
        '<header class="masthead"><div class="eyebrow">'
        f'<span>{md(session.get("repo", ""))}</span>',
    ]
    if number:
        parts.append(f'<span class="sep">/</span><span>pull/{md(number)}</span>')
    parts.append('<span class="sep">·</span><span>underwrite</span>')
    if session.get("date"):
        parts.append(f'<span class="sep">·</span><span>{md(session["date"])}</span>')
    if live:
        parts.append('<span id="live" class="live starting">connecting</span>')
    parts.append(
        f'</div><h1><span class="num">{html.escape(label)}</span> '
        f'{md(session.get("title", ""))}</h1>'
    )
    if session.get("facts"):
        facts = "".join(f"<span>{md(f)}</span>" for f in session["facts"])
        parts.append(f'<div class="facts">{facts}</div>')
    if live:
        parts.append(
            '<div class="acts walk" data-acts="walk">'
            '<button class="act" data-action="next">Next beat</button>'
            '<span class="act-msg"></span></div>'
        )
    parts.append("</header>")

    parts.append('<div id="live-body">')
    parts.append(body_html(session, beats, problems_by_n, live))
    parts.append("</div>")

    if session.get("footer"):
        parts.append(f"<footer>{md(session['footer'])}</footer>")
    parts.append("</div>")
    if live:
        parts.append(LIVE_JS)
    return "\n".join(parts)


def load(root, css_path, final=False):
    """Read a session off disk. Returns (session, beats, problems_by_n, problems)."""
    session = json.loads((root / "session.json").read_text(encoding="utf-8"))
    beats = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((root / "beats").glob("*.json"))
    ]
    css = css_path.read_text(encoding="utf-8")

    problems_by_n, problems = {}, []
    audience = session.get("audience")
    if audience is None:
        mode = "branch"
    elif not isinstance(audience, dict):
        problems.append("session audience must be an object")
        session["audience"] = {}
        mode = "branch"
    else:
        mode = audience.get("mode")
        if mode not in ("branch", "review"):
            problems.append("session audience mode must be branch or review")
            mode = "branch"
    for beat in beats:
        found = validate(beat, mode, final)
        if found:
            problems_by_n[beat.get("n")] = found
            problems += found
    cursor = session.get("cursor")
    if cursor is not None and cursor != len(beats):
        problems.append(f"session cursor is {cursor} but {len(beats)} beat files exist")
    return session, beats, css, problems_by_n, problems


def default_css():
    return Path(__file__).resolve().parent.parent / "assets" / "report.css"


class Usage(argparse.ArgumentParser):
    """argparse exits 2 on a usage error, and 2 already means "rendered, but a beat
    is unproven" to Phase 4. A typo'd flag read as a beat to go and fix, with no page
    on disk to fix it against."""

    def error(self, message):
        sys.exit(f"render-report: {message}")


def main():
    ap = Usage(description="Render an underwrite session to HTML.")
    ap.add_argument("session_dir", help="directory holding session.json and beats/")
    ap.add_argument("--out", help="output file (default <session-dir>/report.html)")
    ap.add_argument("--css", help="override assets/report.css")
    ap.add_argument(
        "--standalone",
        action="store_true",
        help="wrap in a document shell for opening as a local file",
    )
    ap.add_argument(
        "--live",
        action="store_true",
        help="include the polling and decision controls (serve.py uses this)",
    )
    ap.add_argument(
        "--final",
        action="store_true",
        help="require every accepted beat to name its commit or posted review",
    )
    args = ap.parse_args()

    root = Path(args.session_dir).expanduser()
    css_path = Path(args.css).expanduser() if args.css else default_css()
    try:
        session, beats, css, problems_by_n, all_problems = load(
            root, css_path, args.final
        )
    except (OSError, json.JSONDecodeError) as err:
        sys.exit(f"render-report: {err}")

    out = Path(args.out).expanduser() if args.out else root / "report.html"
    page = render(session, beats, css, problems_by_n, args.live)
    out.write_text(SHELL + page if args.standalone else page, encoding="utf-8")

    if all_problems:
        print("render-report: rendered with problems", file=sys.stderr)
        for problem in all_problems:
            print(f"  {problem}", file=sys.stderr)
        print(f"  wrote {out}", file=sys.stderr)
        sys.exit(2)
    print(f"rendered {len(beats)} beats to {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
