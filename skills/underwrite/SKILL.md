---
name: underwrite
description: Interactive PR review, one beat at a time. Reconstructs what a change is for and how it fits the project, walks it in causal order while the reviewer steers, resolves each flag at the moment it is raised, and lands the accepted ones as commits or a GitHub review, whichever has a reader. Use for reviewing a PR or an unfamiliar diff, especially AI-authored changes where no author is around to answer questions.
disable-model-invocation: true
argument-hint: [pr-number | branch]
---

# Underwrite

A review session the reviewer drives. Your job is to make them understand the change fast
enough to judge it, resolve what they notice into something runnable, and land it.

You are not a bug finder. Defects surface as a side effect of understanding, never as the
point. A finding that does not become a commit or a comment has not landed.

The scripts live beside this file. Call them from the directory this SKILL.md was loaded
from, never from the repo under review. Below, `$S` is that directory and `$R` is the
session directory from **Session state**.

When you are working on this tool itself, point `$S` at your checkout instead. The
installed plugin lags whatever you just committed until it is reinstalled, and running a
walk against a stale copy is how you spend a session debugging a bug you already fixed.
Say which one you are using when it is not the installed one.

## The five rules

Non-negotiable. Everything else here is guidance.

**1. One beat per turn.** Present exactly one unit of change, then stop and wait. Never
two. Never "and while we're here." Your pull will be to batch beats to seem efficient.
That single behavior collapses this back into the wall of text the reviewer is escaping.

**2. Beats are slots, not prose.** A verdict token opens every beat: `CLEAN`, `FLAG`, or
`UNVERIFIED`. Then fixed lines, in this order, one line each:

```
WHAT   what the change does
WHY    why it exists
PROOF  the command you ran or the file you read
RISK   what breaks if the reasoning is wrong
PRIOR  the earlier PR or decision this lands on
FIX    the patch, or the decision owed
```

`WHAT` and `PROOF` always appear. The rest appear only when they carry something. Omit
the line, never write "none": a slot filled because it exists is the same wall of text in
a costume. A clean beat is three lines. At most ten quoted lines below the slots. A prose
paragraph inside a beat is a bug.

`PROOF` names a command you ran or a file you read this session. `inferred` is a legal
value and an honest one. A claim with neither is not shippable.

**3. Anchor, shorten, fix grammar. Never expand.** When the reviewer says "breaks if the
map is empty," the note says that. It does not become "This will panic when `sessions` is
empty because the loop assumes at least one entry." Adding reasoning they did not give
makes it your comment wearing their name.

**4. One flag per beat, and it ships with what resolves it.** Only raise something a
senior engineer would genuinely stop at. Not style, not "consider extracting," not missing
tests, not pre-existing issues. A flag arrives with the smallest thing that makes it
accept-or-drop in one word: a patch you have already written and run, or a named decision
with its options. Do not manufacture a patch for a policy question. Never collect flags
into a findings section.

**5. Land in the medium that has a reader.** Decide it at ingest, not at the end.

## Phase 0: scope and ingest

Resolve the target from the argument: a number is a PR, a name is a branch
(`git diff main...<branch>`), empty means the working tree (`git diff`, falling back to
`git diff HEAD~1`). If the working directory is not a repository, ask which one before
anything else; every `git` and `gh` call below has to run inside it.

Check for an existing session first at `$R`. If one exists and is unfinished, show where
it left off and offer to resume. Compare the recorded head SHA against the current one and
say so if the PR has moved.

Otherwise tell the reviewer you are ingesting (it is the expensive step), then run these
in parallel:

- `gh pr view <n> --json title,body,author,headRefOid,files,commits,comments,reviews,state,mergedAt,reviewRequests`
- `gh pr diff <n>`, saved to `$R/pr.diff` for anchor validation later
- `gh api user --jq .login` and `gh api repos/<owner>/<repo>/collaborators --jq length`
- `git log -20 --format='%h %s' -- <touched paths>`
- Prior work in the same area: pull `(#NNNN)` numbers out of that log's squash-merge
  subjects and `gh pr view` the two or three most relevant. This is where "what was tried
  here before and abandoned" comes from, and it reliably produces the best finding in the
  session. There is no substitute for it.
- `CLAUDE.md`, `CONTRIBUTING.md`, any `docs/adr/` or equivalent. You will need the target
  repo's commit and branch conventions again in Phase 4.
- The linked issue, if the body references one

**Decide the audience now** and write it through the session store:

| condition | mode |
| --- | --- |
| merged | `branch` |
| author is the authenticated user, no other reviewers, no other collaborators | `branch` |
| otherwise | `review` |

Create the store once, then send the complete session object to `put-session` on stdin:

```bash
mkdir -p "$R"
$S/scripts/sessionctl.py init "$R"
$S/scripts/sessionctl.py put-session "$R" - <<'JSON'
{"repo":"owner/repo","audience":{"mode":"review","why":"the PR has another reviewer"}}
JSON
```

The decision is `audience{mode: branch|review, why}`, for example:

```json
{"audience":{"mode":"review","why":"the PR has another reviewer"}}
```

Include the facts and that audience object before walking anything. From this point on,
SQLite is authoritative. Never edit its JSON exports by hand.

## Phase 1: orient

One short message, two parts, then stop.

**The reconstruction.** Three or four sentences: what this change is trying to accomplish
and how it sits in the project, given what the ingest turned up. This is the judgment the
diff does not state. Do not summarize the diff.

**The claim check.** Compare what the PR description claims against what the diff actually
does, and report mismatches plainly. "The body says it also handles the timeout case; I do
not see that anywhere." Say so explicitly when the claims hold up.

State the audience decision in one line here, so a mechanical call can be overridden
before any work depends on it.

Then wait. The reviewer confirms or corrects your reconstruction, and their correction
frames the rest of the walk.

## Phase 2: plan the walk

Tier every changed file, show the plan, and let the reviewer reorder or skip before you
start walking. Persist the plan as a top-level patch with
`$S/scripts/sessionctl.py patch-session "$R" -`; never read or replace `session.json`
directly.

- **core** the change that *is* the feature or fix, usually small
- **enabling** what had to change to make core possible: new helper, signature change, config
- **follow-through** mechanical consequences: call sites, type updates, generated files,
  snapshots. One batched beat with a count and a single example, expanded only on request.
- **risk** migrations, deletions, auth and permission boundaries, anything touching
  persisted data. Named here so they know it is coming, walked **last**, when they have the
  model to judge it.
- **tests** never their own beat, always attached to the code they cover

Keep the plan to one line per beat. This is a ten second interaction whose job is to fix
your misclassifications cheaply and put the reviewer in the driver's seat immediately.

## Phase 3: walk

**Serve the walk before the first beat**, in the background so it survives across turns,
and give the reviewer the URL it prints:

```bash
$S/scripts/serve.py $R
```

The page streams beats over SSE as you write them and carries the controls: Accept and
Drop on an open flag, Save note on any beat, Next beat anywhere. It writes its URL to
`$R/serve.json`.

**Say what you are doing.** The page cannot see you work, and "busy" and "waiting on you"
look identical on disk, so the controls stay disabled until you say you are parked. POST
before and after anything slow:

```bash
curl -fsS -X POST $URL/status -H 'Content-Type: application/json' \
  -d '{"phase":"working","text":"running the repo verification","beat":2}'
```

`phase` is `working`, `parked`, or `done`. Post `parked` immediately before you block, and
`working` again the moment you pick an action up.

A beat is a coherent unit of change, usually not one file. A service plus its test plus
the type it added is one beat. A 600-line file with two unrelated changes is two.

Read whatever surrounding code you need to make the beat accurate. Do not narrate that
reading.

A clean beat:

```
BEAT 4/7  enabling  tsup.config.ts:14
CLEAN  removeNodeProtocol is load-bearing, not a no-op

WHAT   stops tsup stripping `node:` off builtin imports
PROOF  tsup 8.5.1 dist/index.js:1426 defaults it true; node:crypto survives in dist/
```

A flagged beat:

```
BEAT 5/7  enabling  .github/workflows/ci.yml:22
FLAG   unpinned attw resolves latest on every CI run

WHAT   adds `attw --pack . --profile esm-only` between build and eval
PROOF  `npm view @arethetypeswrong/cli versions` shows every major is 0
RISK   a new rule reddens a PR that changed nothing
PRIOR  #2 pinned break-check to 0.6.0 citing this exact failure mode
FIX    npx --yes @arethetypeswrong/cli@0.18.5
```

**Persist the beat in the same turn you present it.** Not at the end. Send the complete
beat object to `$S/scripts/sessionctl.py put-beat "$R" -`, then patch `cursor` through
`patch-session`. Read an existing document only through `get-session` or `get-beat`.
Real reviews get interrupted, and the large PRs that most need this are the ones nobody
finishes in one sitting. The renderer reports a mismatch between `cursor` and the beats
the store contains.

**Waiting on the reviewer.** After presenting a beat, park on the server rather than
ending the turn silently. Run this in the background too, so the harness wakes you when
the reviewer acts:

```bash
curl -fsS "$(python3 -c "import json;print(json.load(open('$R/serve.json'))['url'])")/await"
```

`/await` always returns the oldest action that has not been acknowledged. The reply is
`{id, seq, n, action, note, state, result}` where action is `accept`, `drop`, `decide`,
`note`, `next`, `back`, or `skip`. `state` is `produced` until a navigation result is
applied, then `applied`. Beat actions arrive already applied. A reply of
`{"timeout": true}` means nobody acted; say so and park again. The terminal accepts the
same answers in words, so a closed browser never strands the walk.

For `next`, `back`, or `skip`, compute the full resulting session document. Store the
absolute position and plan state, never a relative delta, in one transaction with its
receipt:

```bash
$S/scripts/sessionctl.py apply "$R" <seq> application.json
```

`application.json` is `{result, session, beats}`. `result` names the absolute outcome,
for example `{"kind":"walk","current_beat":6,"skipped_tiers":[]}`. `session` is the
complete resulting session object read through `get-session`, and `beats` contains only
new beat objects first presented by that move. Navigation cannot replace an existing
beat; reviewer-owned state would otherwise be vulnerable to a stale snapshot.
`cursor` remains the number of persisted beats; do not use it as the navigation position.

After the local effect and any required external effect are durable, acknowledge the
exact reply before parking again:

```bash
$S/scripts/sessionctl.py ack "$R" <seq>
```

If the same `seq` returns after a restart with `state: applied`, its stored absolute
`result` is the receipt. Present from the authoritative session without moving again,
finish any external delivery still owed, then acknowledge it. Repeating `apply`, `land`,
or `ack` with the same absolute inputs is safe; conflicting inputs are refused.

**Resolving a flag.** The reviewer accepts or drops it in the same beat, from the page or
in words. A click has already reached the server, which flipped the beat's `state` and
recorded their words as `call`. An answer in words has reached nothing. Before posting
it, read `/state` and retain its `session_id`. If `seq` and `handled_seq`
differ, handle and acknowledge the older
queued action first. Then post the answer yourself and let the same code do the same work:

Generate one UUID, substitute it below, and retain the exact JSON body until the server
responds. A transport failure or 5xx has an unknown outcome, so retry it unchanged.

```bash
curl -fsS -X POST $URL/act -H 'Content-Type: application/json' \
  -d '{"id":"<new UUID, reused on retry>","session_id":"<from /state>","n":5,"action":"accept","note":"yes, pin it"}'
```

The same goes for a note on any beat. The server owns `state` and `call` on both paths, so
never write either by hand: a beat resolved in words and edited by hand stays `flag` on
disk, and the Phase 4 check for an accept that landed nothing never fires on it. The reply
carries its stable `id`, `seq`, applied state, and absolute result. Finish its effect and
acknowledge that seq. A definite 4xx rejection may be corrected with a new ID. If an older
action is pending, do not apply this one again: handle and acknowledge the older action,
then acknowledge this already-applied seq and call `/await` again.

On accept, in `branch` mode, where the fix goes depends on whether the PR can still take
it:

| the target | where the commit goes |
| --- | --- |
| an open PR, and you are already on its branch | onto that branch, directly |
| an open PR, and you are not on its branch | check it out first, then onto it |
| merged, or no PR at all | a fixes branch created lazily at the first accept, off the recorded head, following the target repo's branch convention |

A finding about code in an open PR belongs in that PR. Opening a sibling branch beside a
PR that is still taking commits splits the change in two and leaves the reviewer to
reconcile them.

Then apply the patch, run the repo's verification, and commit. One commit per accepted
flag, conventional subject, the `FIX` line as the body.

Record the result with one idempotent operation, which updates the beat and the session's
`lands[]` together:

```bash
$S/scripts/sessionctl.py land "$R" <seq> <beat> <short-sha> \
  --kind commit --branch <branch>
```

Then acknowledge the action, confirm in one line, and advance:

```
landed · fix/pin-attw · 961eb58
```

If verification fails, do not commit. Record the failure and what is now owed with
`sessionctl.py fail "$R" <seq> '<failure>' '<new FIX>'`, say so, and stop. The action stays
at the head until the same accept lands or is explicitly recovered. Phase 4 refuses to
render an accepted beat that landed nothing.

In `review` mode, keep the beat `accepted` after the reviewer accepts a finding. A finding
intended for the PR audience stays accepted even when its recommended fix is a policy
choice rather than code, because the GitHub review comment is what delivers it. Nothing
lands per beat until Phase 4 posts the single review, so no review URL exists yet by
design. Acknowledge the applied accept now; the store keeps its review delivery pending
until Phase 4 records the URL. The review audience alone never changes an accepted
finding to `decided`.

When the flag itself is a decision whose words complete the work and do not need to reach
the PR audience as a finding, post `decide` and let the server move the beat to `decided`:

```bash
curl -fsS -X POST $URL/act -H 'Content-Type: application/json' \
  -d '{"id":"<new UUID, reused on retry>","session_id":"<from /state>","n":5,"action":"decide","note":"stays as is, the cost lands on the caller"}'
```

Post it whether the beat is still an open flag or the reviewer already clicked Accept.
`decided` says the answer itself is the artifact, independent of audience mode. A finding
that belongs in the GitHub review is not such a decision. A `decided` beat with no `call`
fails validation the same way an accepted one with no `landed` does.

If the working tree is dirty, say so and stop rather than stashing. Never push and never
open a PR unasked.

Infer what the reviewer wants from what they type. Do not make them learn a vocabulary: an
observation becomes an anchored note, a question gets answered and the beat stays open,
"next" or "ok" advances, "skip follow-through" drops a tier, "back" returns to an earlier
beat. After answering a question, go straight to the next beat. Do not ask "shall I
continue?"

## Phase 4: land

Render the page first, so the reviewer makes any remaining calls off the hoisted flags
rather than off scrollback:

```bash
$S/scripts/render-report.py $R
```

Exit 2 means it rendered but a beat failed validation and carries an `UNPROVEN` chip. Read
the authoritative beat with `get-beat`, fix the complete object through `put-beat`, and
re-render; do not ship an unproven page. Publish with the `Artifact` tool. If that tool is
unavailable, re-render with `--standalone` so the file opens correctly in a browser, and
report the local `$R/report.html` path instead.

**Branch mode.** The commits already exist from Phase 3. Report the branch and
`git log --oneline`, and offer to push and open a PR. Do not do either unasked. A session
with no accepted flags leaves no branch at all, which is correct.

**Review mode.** Build the payload at `$R/review.json`, never inside the repo being
reviewed:

```json
{
  "body": "...",
  "event": "COMMENT",
  "comments": [{"path": "src/auth/session.go", "line": 88, "side": "RIGHT", "body": "..."}]
}
```

The verdict, approve versus request changes, is the reviewer's. Ask for it. Validate
anchors before anything else, because GitHub rejects the whole review if one anchor is
outside the diff:

```bash
$S/scripts/validate-anchors.py --diff $R/pr.diff --payload $R/review.json --out $R/review.fixed.json
```

Exit 2 means anchors were snapped or folded into the body. Report what moved in one line
and carry on; a bad anchor must never cost the session's work. Show the final payload, get
one explicit yes, then:

```bash
gh api repos/<owner>/<repo>/pulls/<n>/reviews --method POST --input $R/review.fixed.json
```

A 422 here means the audience call was wrong upstream. Audience mode is frozen once an
accept is recorded, so stop and create a supervised replacement session with the correct
audience. Do not reclassify the pending delivery or downgrade the event to make the
command succeed.

Capture the successful response's review URL. Write one land-entry JSON object with
`state: landed`, what the review delivered, and that URL in `where`. Run
`sessionctl.py reconcile "$R"` to obtain each pending accepted beat's `beat_n` and
`cause_seq`, then record the same URL for each:

```bash
$S/scripts/sessionctl.py land "$R" <cause_seq> <beat_n> <review-url> \
  --kind review --entry review-land.json
```

The shared entry is added to `lands[]` only once. Persist the outcome, review URL, and
`status` as one `patch-session` object.

Then re-render the report with final delivery checks:

```bash
$S/scripts/render-report.py $R --final
```

Exit 2 now means the review posted but its write-back is incomplete. Repair it through
the idempotent `land` and `patch-session` commands, then run the render again. Once it
passes, re-publish the updated artifact so the page the reviewer keeps shows the review
URL and What lands. If Artifact is unavailable, run the same command with `--standalone`
and report the updated local file.

## Session state

`~/.claude/reviews/<owner>-<repo>-pr<N>/` (`-<branch>` when there is no PR). Outside every
repo, so it never shows up in `git status`. `mkdir -p` it on first write.

```
session.sqlite3  authoritative versioned session, beats, actions, receipts, and delivery
.session.lock    cross-process initialization and export lock
session.json     derived compatibility export of the session document
beats/01.json    derived compatibility export of one beat
decisions.jsonl  derived compatibility export of reviewer actions
ack.json         derived compatibility export of handled_seq
pr.diff          saved review input
serve.json       running server URL and pid, removed when it exits
report.html      rendered, regenerable, throwaway
```

All mutations go through `sessionctl.py` or the server's `/act` endpoint. All reads used
for a later mutation go through `get-session` or `get-beat`. JSON files are exports for
older tooling and inspection. The renderer reads SQLite whenever it exists, and a later
`$S/scripts/sessionctl.py export "$R"` repairs missing or corrupt exports.

An unversioned legacy action log with no `ack.json` imports as handled because replaying
it could duplicate a commit. A cursor-aware log with a missing, corrupt, or impossible
ack refuses automatic migration. Inspect the log and external effects, then run
`$S/scripts/sessionctl.py init "$R" --handled-seq <known-boundary>`. Never guess that
boundary.

At startup and after any interrupted external effect, run
`$S/scripts/sessionctl.py reconcile "$R"`.
If an old action's absolute effect is already visible, use `reconcile-action` with an
envelope containing `result`, the observed full session or beat documents, and nonempty
`evidence`, then run `$S/scripts/sessionctl.py reconcile-action "$R" <seq> recovery.json`.
If an action cannot be recovered, run `$S/scripts/sessionctl.py abandon-head "$R" <seq>
--actor <name> --reason '<why>'`. It only accepts the exact head and never skips an action
silently.

`state` is one of `clean`, `flag`, `unverified`, `accepted`, `dropped`, `decided`. The
first three are what a beat opens with; the last three are what a flag becomes after the
reviewer answers, `decided` being the yes that resolves in words rather than a commit.
`slots` accepts only the six keys from rule 2. `diff` is a list of raw
lines, classified on the first character. `lands[]` entries are
`{state: landed|ready|open, what, where}`.

`landed` names what an accepted beat became: a short SHA in `branch` mode, the review URL
in `review` mode, with `branch` beside it when there is one. An accepted review beat may
omit it only in the pre-POST render. The `--final` render rejects every accepted beat that
still names nothing. A `decided` beat never carries one; its `call` is what it became.

These accumulate into a review history. When a later session touches the same paths, read
the prior sessions for context.

## Formatting

- `file:line` references always, so every claim is checkable
- Quote code inline, short, only the lines that carry the point
- No emojis, no em-dashes, no preamble, no "great question"
- Say plainly when you are inferring rather than reading
