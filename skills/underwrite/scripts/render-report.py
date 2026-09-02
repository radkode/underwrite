#!/usr/bin/env python3
"""
Render an underwrite session into a self-contained report page.

Reads the authoritative session database, falling back to legacy JSON, inlines
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
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from session_store import (
    AUDIENCE_MODES,
    BEAT_SLOTS as SLOTS,
    SessionStore,
    StoreError,
    validate_beat,
)

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
  const body = document.getElementById('live-body');
  const NAVIGATION = new Set(['next', 'back', 'skip']);
  let liveClass = 'live starting', liveText = 'connecting';
  let between = false, lastPhase = null, done = false;
  const pendingKey = 'underwrite.pending-action';
  let rev = null, known = new Set(), usable = false, connected = false, sessionId = null;
  let sending = false, pending = null;
  let desiredRev = null, swapPromise = null, swapRetry = null, resumedPending = false;
  let retryWhenIdle = false;
  let observedSeq = 0, awaitingSeq = null;

  function paintLive(cls, text) {
    if (cls !== undefined) { liveClass = cls; liveText = text; }
    const live = document.getElementById('live');
    if (live) { live.className = liveClass; live.textContent = liveText; }
  }

  // A Next beat that is produced but not yet applied means the walk is between beats:
  // the stage shows the ghost of what is coming and the beat that just left folds up.
  function paintStage() {
    const stage = document.getElementById('stage');
    if (!stage) return;
    stage.classList.toggle('is-between', between);
    const ghost = stage.querySelector('.ghost');
    const card = stage.querySelector('.beat');
    if (ghost && card) ghost.hidden = !between;
    if (card && between) card.open = false;
  }

  try { pending = JSON.parse(sessionStorage.getItem(pendingKey)); } catch (_) {}
  if (!pending || typeof pending.id !== 'string'
      || typeof pending.session_id !== 'string') {
    pending = null;
    try { sessionStorage.removeItem(pendingKey); } catch (_) {}
  }

  function remember(value) {
    pending = value;
    try {
      if (value) sessionStorage.setItem(pendingKey, JSON.stringify(value));
      else sessionStorage.removeItem(pendingKey);
    } catch (_) {}
  }

  const beatIds = () => new Set([...document.querySelectorAll('.beat')].map(d => d.dataset.n));
  const openIds = () => new Set([...document.querySelectorAll('.beat[open]')].map(d => d.dataset.n));

  const rowKey = value => value.n === null ? 'walk' : String(value.n);
  const sameAction = (left, right) => left.session_id === right.session_id
    && left.n === right.n
    && left.action === right.action && left.note === right.note;
  const actionRow = value => [...document.querySelectorAll('.acts')]
    .find(row => row.dataset.acts === rowKey(value));

  function showMessage(value, text) {
    const row = actionRow(value);
    const msg = row && row.querySelector('.act-msg');
    if (msg) msg.textContent = text;
  }

  function restorePending() {
    if (!pending || pending.session_id !== sessionId) return;
    const row = actionRow(pending);
    const note = row && row.querySelector('.note');
    if (note) note.value = pending.note || '';
    showMessage(pending, 'retry pending ' + pending.action);
  }

  // Acting mid-action races the walk, so the controls close while one is running. They
  // stay open when no walk is listening: the call is appended either way, and a page
  // that goes dead the moment nobody is home is how a session looks broken when it is
  // only unattended.
  function applyState(state) {
    const incomingSessionId = typeof state.session_id === 'string' && state.session_id
      ? state.session_id : null;
    if (!incomingSessionId) {
      usable = false;
      enable(false);
      return;
    }
    if (sessionId && sessionId !== incomingSessionId) {
      remember(null);
      sessionId = null;
      usable = false;
      enable(false);
      location.reload();
      return;
    }
    sessionId = incomingSessionId;
    if (pending && pending.session_id !== sessionId) remember(null);
    const status = state.status || {};
    const phase = status.phase || 'working';
    const listening = state.listening !== false;
    const queued = state.seq !== state.handled_seq;
    observedSeq = state.seq;
    if (awaitingSeq !== null && observedSeq >= awaitingSeq) awaitingSeq = null;
    if (pending && state.head_id === pending.id) remember(null);
    // Done outranks away: a finished walk is not an unattended one, and its last
    // word stays on the page. Beat controls stay open, since Phase 4 has the
    // reviewer make any remaining calls off this page; only Next beat closes.
    done = phase === 'done';
    usable = !queued && (listening ? phase === 'parked' : true);
    paintLive(
      'live ' + (done ? 'done' : listening ? phase : 'away'),
      done || listening
        ? (status.text || phase)
        : 'no walk is listening, your call is saved for whenever one returns'
    );
    between = queued && NAVIGATION.has(state.head_kind) && state.head_state === 'produced';
    paintStage();
    // Nothing in the store changes when the agent says done, so the stage would stay;
    // one forced refetch lets the server render the ledger alone.
    if (phase === 'done' && lastPhase !== 'done' && lastPhase !== null) {
      rev = null;
      requestSwap(state.rev);
    }
    lastPhase = phase;
    if (!sending) enable(connected && usable && awaitingSeq === null);
  }

  const enable = on => document.querySelectorAll('.act, .note')
    .forEach(el => el.disabled = !on || (done && el.dataset.action === 'next'));

  async function swap(targetRev) {
    const response = await fetch('./fragment');
    if (!response.ok) throw new Error(await response.text() || response.status);
    const fragment = await response.text();
    const open = openIds();
    const drafts = new Map([...document.querySelectorAll('.acts')].map(row => {
      const note = row.querySelector('.note');
      return [row.dataset.acts, note ? note.value : null];
    }));
    const focused = document.activeElement && document.activeElement.closest('.acts');
    const focusKey = focused ? focused.dataset.acts : null;
    const selection = document.activeElement && document.activeElement.classList.contains('note')
      ? [document.activeElement.selectionStart, document.activeElement.selectionEnd]
      : null;
    body.innerHTML = fragment;
    document.querySelectorAll('.beat').forEach(d => {
      if (open.has(d.dataset.n)) d.open = true;
      if (!known.has(d.dataset.n)) d.classList.add('is-new');
    });
    document.querySelectorAll('.acts').forEach(row => {
      const note = row.querySelector('.note');
      if (note && drafts.has(row.dataset.acts) && drafts.get(row.dataset.acts) !== null) {
        note.value = drafts.get(row.dataset.acts) || '';
      }
      if (note && row.dataset.acts === focusKey) {
        note.focus();
        if (selection) note.setSelectionRange(selection[0], selection[1]);
      }
    });
    known = beatIds();
    wire();
    paintLive();
    paintStage();
    restorePending();
    enable(!sending && connected && usable && awaitingSeq === null);
    rev = targetRev;
  }

  function requestSwap(targetRev) {
    desiredRev = targetRev;
    if (swapPromise || rev === desiredRev) return;
    enable(false);
    swapPromise = swap(desiredRev)
      .catch(() => {})
      .finally(() => {
        swapPromise = null;
        if (connected && rev !== desiredRev) {
          clearTimeout(swapRetry);
          swapRetry = setTimeout(() => requestSwap(desiredRev), 250);
        }
      });
  }

  async function sendAction(payload) {
    if (sending || !connected || !usable || awaitingSeq !== null
        || payload.session_id !== sessionId) return false;
    remember(payload);
    sending = true;
    enable(false);
    showMessage(payload, 'sending');
    let rejected = false;
    try {
      const sent = await fetch('./act', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!sent.ok) {
        const detail = (await sent.text()).trim() || sent.status;
        rejected = sent.status >= 400 && sent.status < 500;
        if (rejected) remember(null);
        throw new Error(detail);
      }
      const receipt = await sent.json();
      awaitingSeq = Number.isInteger(receipt.seq) && receipt.seq > observedSeq
        ? receipt.seq : null;
      remember(null);
      showMessage(payload, '');
    } catch (err) {
      showMessage(
        payload,
        rejected || (pending && pending.id === payload.id)
          ? 'not sent, ' + err.message : 'saved'
      );
    } finally {
      sending = false;
      enable(connected && usable && awaitingSeq === null);
      if (retryWhenIdle) {
        retryWhenIdle = false;
        resumePending();
      }
    }
    return true;
  }

  function resumePending() {
    if (!pending || pending.session_id !== sessionId || resumedPending
        || !connected || !usable || awaitingSeq !== null) return;
    if (sending) {
      retryWhenIdle = true;
      return;
    }
    resumedPending = true;
    sendAction(pending);
  }

  async function act(button) {
    const row = button.closest('.acts');
    const n = row.dataset.acts;
    const note = row.querySelector('.note');
    const msg = row.querySelector('.act-msg');
    const fresh = {
      id: crypto.randomUUID(),
      session_id: sessionId,
      n: n === 'walk' || button.dataset.action === 'next' ? null : +n,
      action: button.dataset.action,
      note: note ? note.value.trim() : '',
    };
    if (fresh.action === 'decide' && !fresh.note) {
      msg.textContent = 'enter the decision first';
      if (note) note.focus();
      return;
    }
    if (pending && !sameAction(pending, fresh)) {
      msg.textContent = 'retry the saved ' + pending.action + ' first';
      restorePending();
      return;
    }
    await sendAction(pending || fresh);
  }

  const wire = () => {
    document.querySelectorAll('.act').forEach(b => b.onclick = () => act(b));
    if (!/Mac|iPhone|iPad/.test(navigator.platform)) {
      document.querySelectorAll('[data-mod]').forEach(k => k.textContent = 'Ctrl+');
    }
  };

  // Enter in the note saves it, or records a decision when the note is the decision;
  // ⌘/Ctrl+Enter fires the row's primary; outside a field, n is Next beat and f goes to
  // the first open flag. Enter never includes or implements on its own: the note field
  // is optional there, and a keystroke that reads as "save this" must not be terminal.
  document.addEventListener('keydown', event => {
    const target = event.target;
    if (target && target.classList && target.classList.contains('note')) {
      if (event.key !== 'Enter') return;
      const row = target.closest('.acts');
      const primary = row.querySelector('.act.primary');
      const modified = event.metaKey || event.ctrlKey;
      const usePrimary = primary && (modified || primary.dataset.action === 'decide');
      const button = usePrimary ? primary : row.querySelector('.act[data-action="note"]');
      if (button && !button.disabled) { event.preventDefault(); act(button); }
      return;
    }
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;
    if (event.key === 'n') {
      const next = [...document.querySelectorAll('.act[data-action="next"]')].find(b => !b.disabled);
      if (next) { event.preventDefault(); act(next); }
    } else if (event.key === 'f') {
      const flag = document.querySelector('.beat.s-flag');
      if (!flag) return;
      flag.open = true;
      flag.scrollIntoView({ block: 'start' });
      (flag.querySelector('.act.primary') || flag.querySelector('summary')).focus();
    }
  });

  const stream = new EventSource('./events');
  stream.onmessage = event => {
    connected = true;
    const state = JSON.parse(event.data);
    applyState(state);
    resumePending();
    requestSwap(state.rev);
  };
  stream.onerror = () => {
    connected = false;
    resumedPending = false;
    paintLive('live down', 'reconnecting');
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


def session_mode(session):
    audience = session.get("audience")
    if isinstance(audience, dict) and audience.get("mode") in AUDIENCE_MODES:
        return audience["mode"]
    return "branch"


def execution_mode(session):
    policy = session.get("execution_policy")
    if isinstance(policy, dict) and policy.get("mode") == "no_exec":
        return policy["mode"]
    legacy_pr = session.get("legacy_pr")
    if (
        "target" not in session
        and isinstance(legacy_pr, dict)
        and isinstance(legacy_pr.get("number"), int)
        and not isinstance(legacy_pr.get("number"), bool)
        and legacy_pr["number"] > 0
    ):
        return "no_exec"
    return None


def frozen_pr_mode(session):
    target = session.get("target")
    if not isinstance(target, dict) or target.get("kind") != "github_pr":
        return None
    if target.get("state") == "open" and target.get("merged_at") is None:
        return "review"
    return "report"


def replacement_required(session):
    target = session.get("target")
    expected = frozen_pr_mode(session)
    if session_mode(session) == "report" and expected != "report":
        return True
    if expected is not None:
        return (
            "trusted_context_sha256" not in target
            or session_mode(session) != expected
        )
    return (
        target is None
        and isinstance(session.get("legacy_pr"), dict)
    )


def validate(beat, mode="branch", final=False):
    """Return a list of problems. Empty means the beat is shippable."""
    return validate_beat(beat, mode, final)


def diff_html(lines):
    out = []
    for line in lines:
        head = line[:1]
        kind = {"+": "add", "-": "del", " ": "ctx"}.get(head, "file")
        out.append(f'<span class="l {kind}">{html.escape(line, quote=False)}</span>')
    return f'<div class="diff"><pre>{"".join(out)}</pre></div>'


def delivery_html(beat, mode, replacement=False, untrusted_pr=False):
    if beat.get("state") != "accepted":
        return ""
    delivery = beat.get("delivery")
    if (
        mode == "report"
        and not any(field in beat for field in ("landed", "branch", "delivery_kind"))
        and (
            not isinstance(delivery, dict)
            or delivery.get("state") in (None, "none")
        )
    ):
        return (
            '<div class="call delivery"><span class="lbl">'
            "Included in report</span></div>"
        )
    if not isinstance(delivery, dict):
        return ""
    state = delivery.get("state")
    blocked_delivery = (
        replacement
        or (
            untrusted_pr
            and delivery.get("kind") == "commit"
        )
    ) and state in ("pending", "failed")
    if blocked_delivery:
        warning = (
            "Do not publish or execute this delivery. Start a supervised replacement."
            if replacement
            else "Do not execute target code. Start a supervised replacement."
        )
        detail = [
            f'<span class="delivery-error">{warning}</span>'
        ]
        if delivery.get("error"):
            detail.append(
                '<span class="delivery-error">Recorded failure: '
                f'{md(delivery["error"])}</span>'
            )
        if delivery.get("owed"):
            detail.append(
                '<span class="delivery-owed">Previously recorded obligation: '
                f'{md(delivery["owed"])}</span>'
            )
        return (
            '<div class="call delivery failed">'
            '<span class="lbl">Blocked, replacement required</span>'
            f'{" ".join(detail)}</div>'
        )
    if state == "pending":
        label = {
            "branch": "Implementation pending",
            "review": "Included, review pending",
            "report": "Report inclusion pending",
        }[mode]
        return (
            '<div class="call delivery pending">'
            f'<span class="lbl">{label}</span></div>'
        )
    if state != "failed":
        return ""
    label = {
        "branch": "Implementation failed",
        "review": "Review publication failed",
        "report": "Report inclusion failed",
    }[mode]
    detail = []
    if delivery.get("error"):
        detail.append(f'<span class="delivery-error">{md(delivery["error"])}</span>')
    if delivery.get("owed"):
        detail.append(
            '<span class="delivery-owed">Next attempt: '
            f'{md(delivery["owed"])}</span>'
        )
    return (
        '<div class="call delivery failed">'
        f'<span class="lbl">{label}</span>{" ".join(detail)}</div>'
    )


def beat_html(
    beat,
    problems,
    expanded,
    live=False,
    mode="branch",
    replacement=False,
    untrusted_pr=False,
    stage=False,
):
    suffix, token = STATE_STYLE.get(beat.get("state"), ("unver", "UNVERIFIED"))
    raw_slots = beat.get("slots")
    slots = raw_slots if isinstance(raw_slots, dict) else {}
    n = beat.get("n", "?")

    chip = '<span class="unproven">unproven</span>' if problems else ""
    rows = []
    for key in SLOTS:
        value = slots.get(key)
        if not value:
            continue
        cls = f' class="{key}"'
        rows.append(f"<dt{cls}>{key}</dt><dd{cls}>{md(value)}</dd>")

    body = [f'<dl class="slots">{"".join(rows)}</dl>']
    if beat.get("diff"):
        body.append(diff_html(beat["diff"]))
    if beat.get("call"):
        body.append(
            '<div class="call"><span class="lbl">Your call · beat '
            f'{n}</span><q>{md(beat["call"])}</q></div>'
        )
    delivery = delivery_html(beat, mode, replacement, untrusted_pr)
    if delivery:
        body.append(delivery)
    if beat.get("landed") and mode != "report":
        branch = beat.get("branch")
        body.append(
            '<div class="shipped"><span class="lbl">Landed</span>'
            f'<code>{md(beat["landed"])}</code>'
            + (f"<span>on</span><code>{md(branch)}</code>" if branch else "")
            + "</div>"
        )
    if live:
        flag = beat.get("state") == "flag"
        enter, mod = "saves a note", None
        if flag:
            decision_only = beat.get("resolution_kind") == "decision"
            report_accept_problems = []
            if mode == "report" and not decision_only:
                accepted = dict(beat, state="accepted")
                report_accept_problems = validate(accepted, "report", final=True)
            blocked = not decision_only and (
                replacement
                or (mode == "branch" and untrusted_pr)
                or bool(report_accept_problems)
            )
            if blocked:
                if replacement:
                    message = "PR session requires a supervised replacement"
                elif mode == "branch" and untrusted_pr:
                    message = "No-exec policy blocks implementation"
                else:
                    message = "Complete finding evidence before inclusion"
                controls = (
                    f'<span class="execution-blocked">{message}</span>'
                    '<button class="act" data-action="drop">Drop</button>'
                    '<button class="act" data-action="note">Save note</button>'
                )
                placeholder = (
                    "record a note before replacing this session"
                    if replacement or (mode == "branch" and untrusted_pr)
                    else "record a note while the finding is completed"
                )
            else:
                action = "decide" if decision_only else "accept"
                label = "Record decision" if decision_only else {
                    "branch": "Implement",
                    "review": "Include in review",
                    "report": "Include in report",
                }[mode]
                controls = (
                    f'<button class="act primary" data-action="{action}">{label}</button>'
                    '<button class="act" data-action="drop">Drop</button>'
                    '<button class="act" data-action="note">Save note</button>'
                )
                placeholder = (
                    "record the decision in your own words"
                    if decision_only
                    else "or put it in your own words"
                )
                verb = {"Implement": "implements", "Record decision": "records"}.get(
                    label, "includes"
                )
                if decision_only:
                    enter = "records"
                else:
                    mod = verb
        else:
            controls = '<button class="act" data-action="note">Save note</button>'
            placeholder = "note this for the record"
        # The stage carries its own Next beat so the whole decision happens in one row,
        # and says what the keys do, once, where the reviewer is acting.
        advance = keys = ""
        if stage:
            advance = '<button class="act" data-action="next">Next beat</button>'
            hints = [f"<kbd>Enter</kbd> {enter}"]
            if mod:
                hints.append(f'<kbd><span data-mod>⌘</span>Enter</kbd> {mod}')
            hints.append("<kbd>n</kbd> next beat")
            keys = f'<span class="keys">{" · ".join(hints)}</span>'
        # The note comes first so Tab lands on the primary right after typing.
        body.append(
            f'<div class="acts" data-acts="{attr(n)}">'
            f'<input class="note" aria-label="your words, beat {attr(n)}" placeholder="{placeholder}">'
            f'{controls}{advance}<span class="act-msg" role="status"></span>{keys}</div>'
        )

    return (
        f'<details class="beat s-{suffix}" data-n="{attr(n)}"{" open" if expanded else ""}>'
        f"<summary>"
        f'<span class="b-num">{md(n)}</span>'
        f'<span class="b-tier">{md(beat.get("tier", ""))}</span>'
        f'<span class="b-claim"><span class="state">{token}</span> &nbsp;'
        f'{md(beat.get("claim", ""))}{chip}</span>'
        f'<span class="b-path">{md(beat.get("where", ""))}</span>'
        + (f'<span class="b-call">“{md(beat["call"])}”</span>' if beat.get("call") else "")
        + f"</summary>"
        f'<div class="b-body">{"".join(body)}</div>'
        f"</details>"
    )


def walk_entries(session, beats):
    """One entry per planned or walked beat, by n. A beat that was walked off-plan
    still gets a mark; a planned beat not yet walked has no state."""
    entries = {}
    for item in session.get("plan") or []:
        if isinstance(item, dict) and isinstance(item.get("n"), int):
            entries[item["n"]] = {
                "n": item["n"],
                "tier": item.get("tier", ""),
                "where": item.get("where", ""),
                "state": None,
            }
    for beat in beats:
        n = beat.get("n")
        if not isinstance(n, int):
            continue
        entry = entries.setdefault(n, {"n": n, "tier": "", "where": "", "state": None})
        entry["state"] = beat.get("state")
        entry["tier"] = beat.get("tier") or entry["tier"]
        entry["where"] = beat.get("where") or entry["where"]
    return [entries[n] for n in sorted(entries)]


def bar_html(session, beats, current, phase):
    """The sticky strip: the plan as a track, the agent's status, and Next beat."""
    entries = walk_entries(session, beats)
    total = len(entries)
    current_n = current.get("n") if current else None
    marks = []
    for entry in entries:
        state = entry["state"]
        if state is None:
            cls, word = "is-todo", "not yet walked"
        else:
            suffix, token = STATE_STYLE.get(state, ("unver", "UNVERIFIED"))
            cls, word = f"s-{suffix}", token.lower()
        now = entry["n"] == current_n
        current_attr = ' aria-current="step"' if now else ""
        title = " · ".join(
            part for part in (
                f"beat {entry['n']}", entry["tier"], entry["where"], word
            ) if part
        )
        marks.append(
            f'<li class="tk {cls}{" is-now" if now else ""}"'
            f'{current_attr} title="{attr(title)}">'
            f"{md(entry['n'])}</li>"
        )
    if current_n:
        position = f"beat {current_n} of {total}" if total else f"beat {current_n}"
    elif phase == "done":
        position = f"{len(beats)} of {total} walked" if total else f"{len(beats)} walked"
    else:
        position = "waiting for beat 1"
    return (
        '<div class="bar">'
        f'<ol class="track" aria-label="{attr(position)}">{"".join(marks)}</ol>'
        '<span id="live" class="live starting" role="status">connecting</span>'
        '<div class="acts walk" data-acts="walk">'
        '<button class="act" data-action="next">Next beat</button>'
        '<span class="act-msg" role="status"></span></div>'
        "</div>"
    )


def stage_html(session, beats, current, card):
    """Where the walk is right now: the current beat, and a ghost of the next planned
    one that the client reveals while a Next beat is queued."""
    entries = walk_entries(session, beats)
    total = len(entries)
    current_n = current.get("n") if current else 0
    upcoming = next((e for e in entries if e["n"] == current_n + 1), None)
    if current:
        hint = " · ".join(
            part for part in (
                f"beat {current_n} of {total}" if total else f"beat {current_n}",
                current.get("tier", ""),
            ) if part
        )
    else:
        hint = "nothing walked yet"
    ghost = ""
    if upcoming:
        line = " · ".join(
            part for part in (
                f"beat {upcoming['n']} of {total}", upcoming["tier"], upcoming["where"]
            ) if part
        )
        ghost = (
            f'<div class="ghost"{" hidden" if current else ""}>'
            f'<span class="ghost-n">{md(upcoming["n"])}</span>'
            f'<span class="ghost-what">{md(line)}</span>'
            '<span class="ghost-wait">waiting on the walk</span></div>'
        )
    return (
        '<section class="sec stage" id="stage">'
        f'<div class="sec-head"><h2>Now</h2><span class="hint">{md(hint)}</span></div>'
        f"{ghost}{card}</section>"
    )


def body_html(session, beats, problems_by_n, live=False, phase=None):
    """Everything below the masthead. This is what /fragment re-serves on a change."""
    mode = session_mode(session)
    replacement = replacement_required(session)
    target = session.get("target")
    untrusted_pr = (
        isinstance(target, dict) and target.get("kind") == "github_pr"
    ) or isinstance(session.get("legacy_pr"), dict)
    # While a walk is listening, the newest beat is where the reviewer is, so it leads
    # the page on its own; the ledger below holds what has already been walked. A
    # finished walk, and every final render, is the ledger alone.
    current = None
    if live and beats and phase != "done":
        current = max(
            (b for b in beats if isinstance(b.get("n"), int)),
            key=lambda b: b["n"],
            default=None,
        )
    parts = []
    if live:
        parts.append(bar_html(session, beats, current, phase))
        if phase != "done":
            card = beat_html(
                current, problems_by_n.get(current.get("n")), True,
                live=True, mode=mode, replacement=replacement,
                untrusted_pr=untrusted_pr, stage=True,
            ) if current else ""
            parts.append(stage_html(session, beats, current, card))
    ledger = [b for b in beats if b is not current]

    counts = {}
    for beat in beats:
        counts[beat.get("state")] = counts.get(beat.get("state"), 0) + 1

    # One tile per section, so the first four always sum to the last.
    tiles = [
        ("is-clean", counts.get("clean", 0) + counts.get("unverified", 0), "clean"),
        ("is-flag", counts.get("flag", 0), "needs your call"),
        ("is-acc", counts.get("accepted", 0) + counts.get("decided", 0), "accepted"),
        ("is-drop", counts.get("dropped", 0), "dropped"),
        ("is-mute", len(beats), "beats walked"),
    ]
    parts.append(
        '<div class="counts">'
        + "".join(
            f'<div class="count {cls}"><span class="n">{n}</span>'
            f'<span class="k">{k}</span></div>'
            for cls, n, k in tiles
        )
        + "</div>"
    )

    # A state outside the five matches no section, and a beat that matches no section
    # used to render nowhere while still counting in the tiles. The page exists to say
    # what is owed, so an unplaceable beat is shown, not dropped.
    placed = {state for _h, _t, states, _e in SECTIONS for state in states}
    sections = (*SECTIONS, ("Unplaced", "state is not one of the five", None, True))

    for heading, hint, states, expanded in sections:
        if states is None:
            picked = [b for b in ledger if b.get("state") not in placed]
        else:
            picked = [b for b in ledger if b.get("state") in states]
        if not picked:
            continue
        if current and states and "accepted" in states:
            expanded = False
        cards = "".join(
            beat_html(
                b,
                problems_by_n.get(b.get("n")),
                expanded,
                live=live,
                mode=mode,
                replacement=replacement,
                untrusted_pr=untrusted_pr,
            )
            for b in picked
        )
        parts.append(
            f'<section class="sec"><div class="sec-head"><h2>{heading}</h2>'
            f'<span class="hint">{hint}</span></div>{cards}</section>'
        )

    if session.get("lands") and mode != "report":
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


def render(session, beats, css, problems_by_n, live=False, phase=None):
    target = session.get("target") if isinstance(session.get("target"), dict) else {}
    number = session.get("number") or target.get("number")
    repo = session.get("repo") or target.get("repo", "")
    head = session.get("head") or target.get("head_sha", "")
    label = f"#{number}" if number else head[:7]

    parts = [
        f'<title>{html.escape(label)} underwrite · {html.escape(repo)}</title>',
        f"<style>\n{css}\n</style>",
        '<div class="page">',
        '<header class="masthead"><div class="eyebrow">'
        f'<span>{md(repo)}</span>',
    ]
    if number:
        parts.append(f'<span class="sep">/</span><span>pull/{md(number)}</span>')
    parts.append('<span class="sep">·</span><span>underwrite</span>')
    if session.get("date"):
        parts.append(f'<span class="sep">·</span><span>{md(session["date"])}</span>')
    parts.append(
        f'</div><h1><span class="num">{html.escape(label)}</span> '
        f'{md(session.get("title", ""))}</h1>'
    )
    facts_values = list(session.get("facts") or [])
    if session_mode(session) == "report":
        facts_values.append("Outcome: report only")
    policy = session.get("execution_policy")
    execution = execution_mode(session)
    if isinstance(policy, dict):
        policy_mode = {
            "no_exec": "No-exec",
        }.get(policy.get("mode"), str(policy.get("mode", "unknown")))
        facts_values.append(f"Execution: {policy_mode}")
        if policy.get("trust"):
            facts_values.append(f"Trust: {policy['trust']} PR head")
    elif execution == "no_exec":
        facts_values.append("Execution: No-exec, legacy PR")
        facts_values.append("Trust: untrusted PR head")
    if facts_values:
        facts = "".join(f"<span>{md(f)}</span>" for f in facts_values)
        parts.append(f'<div class="facts">{facts}</div>')
    parts.append("</header>")

    parts.append('<div id="live-body">')
    parts.append(body_html(session, beats, problems_by_n, live, phase))
    parts.append("</div>")

    if session.get("footer"):
        parts.append(f"<footer>{md(session['footer'])}</footer>")
    parts.append("</div>")
    if live:
        parts.append(LIVE_JS)
    return "\n".join(parts)


def load(root, css_path, final=False):
    """Read a session off disk. Returns (session, beats, problems_by_n, problems)."""
    legacy = not (root / "session.sqlite3").exists()
    if legacy:
        session = json.loads((root / "session.json").read_text(encoding="utf-8"))
        if session_mode(session) == "report":
            session, beats = SessionStore(root).presentation_snapshot()
            legacy = False
        else:
            beats = [
                json.loads(p.read_text(encoding="utf-8"))
                for p in sorted((root / "beats").glob("*.json"))
            ]
    else:
        session, beats = SessionStore(root).presentation_snapshot()
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
        if audience.get("mode") not in AUDIENCE_MODES:
            problems.append("session audience mode must be branch, review, or report")
        mode = session_mode(session)
    policy = session.get("execution_policy")
    target = session.get("target")
    expected_mode = frozen_pr_mode(session)
    if mode == "report" and expected_mode != "report":
        problems.append("report audience requires a frozen non-open PR target")
    if isinstance(target, dict) and target.get("kind") == "github_pr":
        if policy is None:
            problems.append("PR session has no execution policy")
        elif not isinstance(policy, dict):
            problems.append("session execution_policy must be an object")
        else:
            if policy.get("trust") != "untrusted":
                problems.append("session execution_policy trust must be untrusted")
            if policy.get("mode") != "no_exec":
                problems.append("session execution_policy mode must be no_exec")
        if mode != expected_mode:
            problems.append(
                f"session audience mode {mode} does not match frozen PR lifecycle "
                f"mode {expected_mode}"
            )
    if legacy:
        expected_delivery = {
            "branch": "commit",
            "review": "review",
            "report": None,
        }[mode]
        for beat in beats:
            if (
                expected_delivery is not None
                and beat.get("state") == "accepted"
                and beat.get("landed")
            ):
                beat.setdefault("delivery_kind", expected_delivery)
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
    return HERE.parent / "assets" / "report.css"


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
        help="require every accepted beat to have its audience outcome",
    )
    args = ap.parse_args()

    root = Path(args.session_dir).expanduser()
    css_path = Path(args.css).expanduser() if args.css else default_css()
    try:
        session, beats, css, problems_by_n, all_problems = load(
            root, css_path, args.final
        )
    except (OSError, sqlite3.Error, StoreError, json.JSONDecodeError) as err:
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
