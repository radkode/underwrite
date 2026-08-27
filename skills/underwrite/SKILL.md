---
name: underwrite
description: Interactive PR review, one beat at a time. Reconstructs what a change is for and how it fits the project, walks it in causal order while the reviewer steers, resolves each flag at the moment it is raised, and lands the chosen resolutions as commits, recorded decisions, or a GitHub review, whichever has a reader. Use for reviewing a PR or an unfamiliar diff, especially AI-authored changes where no author is around to answer questions.
disable-model-invocation: true
argument-hint: [pr-number | branch]
---

# Underwrite

A review session the reviewer drives. Your job is to make them understand the change fast
enough to judge it, resolve what they notice into something runnable, and land it.

You are not a bug finder. Defects surface as a side effect of understanding, never as the
point. A finding that does not become a commit, comment, or recorded decision has not
landed.

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
FIX    the implementation intent, review recommendation, or decision owed
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

**4. One flag per beat, with a concrete resolution.** Only raise something a senior
engineer would genuinely stop at. Not style, not "consider extracting," not missing tests,
not pre-existing issues. A branch flag carries the smallest implementation intent that
could resolve it. A review flag carries a finding ready to include in the final review. A
policy question carries a named decision with its options. Never claim code has already
been written or verified before it has, and do not manufacture a patch for a policy
question. Never collect flags into a findings section.

**5. Land in the medium that has a reader.** Decide it at ingest, not at the end.

## Phase 0: scope and ingest

Resolve the target from the argument: a number is a PR, a name is a branch
(`git diff main...<branch>`), empty means the working tree (`git diff`, falling back to
`git diff HEAD~1`). If the working directory is not a repository, ask which one before
anything else; every `git` and `gh` call below has to run inside it.

Check for an existing session first at `$R`. For a PR session, verify its frozen target
before offering to resume:

```bash
$S/scripts/sessionctl.py check-pr "$R"
```

Exit 2 means its base, head, or lifecycle moved. Stop and reconcile into a supervised
replacement session. Never refresh the target or diff inside the existing session. An
operational failure exits 1 and also blocks resumption until it is understood.

Otherwise tell the reviewer you are ingesting (it is the expensive step). For a PR,
initialize the store and capture its exact base and head before starting any contextual
reads:

```bash
mkdir -p "$R"
$S/scripts/sessionctl.py init "$R"
$S/scripts/sessionctl.py snapshot-pr "$R" <owner/repo> <n>
```

`snapshot-pr` reads both SHAs and the head repository and ref from one GitHub response,
fetches the exact base commit and the base repository's `refs/pull/<n>/head` into a fresh
bare repository, verifies both fetched commits, and saves a local three-dot diff. It reads
the PR again before freezing the target. Exit 2 means the PR moved during capture; repeat
the new capture. It never uses a GitHub-rendered diff, whose successful response does not
prove completeness.

After the snapshot is frozen, run these contextual reads in parallel:

- Read `$R/pr.json`, then `gh pr view <n> --json title,body,author,files,commits,comments,reviews,state,mergedAt,reviewRequests`
- `gh api user --jq .login` and `gh api repos/<owner>/<repo>/collaborators --jq length`
- `git log -20 --format='%h %s' -- <touched paths>`
- Prior work in the same area: pull `(#NNNN)` numbers out of that log's squash-merge
  subjects and `gh pr view` the two or three most relevant. This is where "what was tried
  here before and abandoned" comes from, and it reliably produces the best finding in the
  session. There is no substitute for it.
- `CLAUDE.md`, `CONTRIBUTING.md`, any `docs/adr/` or equivalent. You will need the target
  repo's commit and branch conventions again in Phase 4.
- The linked issue, if the body references one

Run `check-pr` again after those reads. The contextual data is usable only while the
frozen base and head still match.

**Decide the audience now** and write it through the session store:

| condition | mode |
| --- | --- |
| merged | `branch` |
| open, its head repository exists, author is the authenticated user, no other reviewers, no other collaborators | `branch` |
| otherwise | `review` |

For a PR, add the audience and other session facts with `patch-session`; the immutable
target already owns its repo, number, base, and head:

```bash
$S/scripts/sessionctl.py patch-session "$R" - <<'JSON'
{"audience":{"mode":"review","why":"the PR has another reviewer"}}
JSON
```

For a branch or working-tree target, initialize the store and send its complete session
object to `put-session` as before.

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

The page streams beats over SSE as you write them and carries mode-specific controls. An
ordinary flag shows Implement in `branch` mode or Include in review in `review` mode, plus
Drop. A decision-only flag with top-level `resolution_kind: "decision"` shows Record
decision. Save note remains available on any beat, and Next beat works anywhere. The page
writes its URL to `$R/serve.json`.

Those labels name the effect while the durable protocol remains stable. Implement and
Include in review both post the canonical `accept` action. Record decision posts the
existing `decide` action. New ordinary flags persist `resolution_kind: "delivery"`.
Imported legacy beats may omit the field and retain their earlier transition rules.

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

**Resolving a flag.** The reviewer chooses the named action or drops the flag in the same
beat, from the page or in words. In branch mode, Implement authorizes applying the stated
`FIX`, running verification, and committing the result after the click. It does not
approve an exact prepared patch. In review mode, Include in review queues the finding for
the final GitHub review. Both clicks have already posted the canonical `accept` action,
which flips the beat's `state` and records the reviewer's words as `call`.

An answer in words has reached nothing. Before posting it, read `/state` and retain its
`session_id`. If `seq` and `handled_seq` differ, handle and acknowledge the older
queued action first. Then post the answer yourself and let the same code do the same work:

Generate one UUID, substitute it below, and retain the exact JSON body until the server
responds. A transport failure or 5xx has an unknown outcome, so retry it unchanged.

```bash
curl -fsS -X POST $URL/act -H 'Content-Type: application/json' \
  -d '{"id":"<new UUID, reused on retry>","session_id":"<from /state>","n":5,"action":"accept","note":"yes, pin it"}'
```

The same goes for a note on any beat. The server owns `state` and `call` on both paths, so
never write either by hand: a beat resolved in words and edited by hand stays `flag` on
disk, and the final Phase 4 check for an accept that landed nothing never fires on it. The
reply carries its stable `id`, `seq`, applied state, and absolute result. Finish its effect
and acknowledge that seq. A definite 4xx rejection may be corrected with a new ID. If an
older action is pending, do not apply this one again: handle and acknowledge the older
action, then acknowledge this already-applied seq and call `/await` again.

On accept, in `branch` mode, the visible action was Implement. Where the resulting fix
goes depends on whether the PR can still take it:

| the target | where the commit goes |
| --- | --- |
| an open PR, and you are already on its branch | onto that branch, directly |
| an open PR, and you are not on its branch | check it out first, then onto it |
| merged, or no PR at all | a fixes branch created lazily at the first accept, off the recorded head, following the target repo's branch convention |

A finding about code in an open PR belongs in that PR. Opening a sibling branch beside a
PR that is still taking commits splits the change in two and leaves the reviewer to
reconcile them.

For a PR target, run `sessionctl.py check-pr "$R"` immediately before checkout or
implementation. Exit 2 stops the effect and requires a supervised replacement session.
Check out the frozen head, not the current branch tip. For a merged PR's first accept,
create the fixes branch there and pin its exact name before editing:

```bash
$S/scripts/sessionctl.py pin-branch "$R" <fixes-branch>
```

An exact retry is safe, but a different name is refused. Then prove the local position:

```bash
$S/scripts/sessionctl.py check-worktree "$R" "$PWD"
```

For an open PR this also requires its frozen head repository to exist and the local branch
to match its frozen head ref. For a merged PR it requires the pinned fixes branch to begin
at the frozen head. After earlier accepted flags, it requires local `HEAD` to equal the
latest commit already recorded by this session. Implement the stated `FIX` and run the
repo's verification.
Run both `check-pr` and `check-worktree` again immediately before the commit, because
verification can be long enough for either the PR or local branch to move. One commit per
accepted flag, conventional subject, the `FIX` line as the body.

```bash
$S/scripts/sessionctl.py check-pr "$R"
$S/scripts/sessionctl.py check-worktree "$R" "$PWD"
```

Record the result with one idempotent operation, which updates the beat and the session's
`lands[]` together:

```bash
SHA=$(git rev-parse HEAD)
$S/scripts/sessionctl.py land "$R" <seq> <beat> "$SHA" \
  --kind commit --branch <branch> --repo-root "$PWD"
```

For a frozen PR, `land` requires the full SHA and proves that it is the worktree's current
`HEAD`, has exactly the prior recorded position as its sole parent, and is on the delivery
branch. An exact retry returns the recorded receipt without depending on later local work.

Then acknowledge the action, confirm in one line, and advance:

```
landed · fix/pin-attw · 961eb58
```

If implementation or verification fails, do not commit. Record the failure and what is
now owed with `sessionctl.py fail "$R" <seq> '<failure>' '<owed>'`, say so, and stop. The
approved `FIX` intent remains unchanged; the failure stores the next attempt separately.
The action stays at the head until the same accept lands or is explicitly recovered.
The page shows Implementation failed, and the Phase 4 final render refuses to ship it.

In `review` mode, Include in review posts `accept`, so keep the beat `accepted`. A finding
intended for the PR audience stays accepted even when its recommended fix is a policy
choice rather than code, because the GitHub review comment is what delivers it. Nothing
lands per beat until Phase 4 posts the single review, so no review URL exists yet by
design. Acknowledge the applied accept now; the store keeps its review delivery pending
until Phase 4 records the URL, and the page shows Included, review pending. The review
audience alone never changes an accepted finding to `decided`.

When the flag itself is a decision whose words complete the work and do not need to reach
the PR audience as a finding, set top-level `resolution_kind: "decision"` before presenting
the beat. Record decision then posts `decide` and moves the beat directly to `decided`. A
terminal answer must post `decide` too:

```bash
curl -fsS -X POST $URL/act -H 'Content-Type: application/json' \
  -d '{"id":"<new UUID, reused on retry>","session_id":"<from /state>","n":5,"action":"decide","note":"stays as is, the cost lands on the caller"}'
```

For a legacy beat already moved to `accepted`, the same `decide` action refines it into the
decision outcome. `decided` says the answer itself is the artifact, independent of
audience mode. A finding that belongs in the GitHub review is not such a decision. A
`decided` beat with no `call` fails validation the same way an accepted one with no
`landed` does.

If the working tree is dirty, say so and stop rather than stashing. Never push and never
open a PR unasked.

Infer what the reviewer wants from what they type. Do not make them learn a vocabulary: an
observation becomes an anchored note, a question gets answered and the beat stays open,
"next" or "ok" advances, "skip follow-through" drops a tier, "back" returns to an earlier
beat. After answering a question, go straight to the next beat. Do not ask "shall I
continue?"

## Phase 4: land

Render the page first, so the reviewer makes any remaining calls off the hoisted flags
rather than off scrollback. Branch delivery is already complete, so render it with final
delivery checks. Review mode needs a pre-POST preview, so omit `--final` until the GitHub
review lands:

```bash
# branch mode
$S/scripts/render-report.py $R --final

# review mode, before the GitHub POST
$S/scripts/render-report.py $R
```

Without `--final`, pending and failed delivery are valid live states and remain visibly
unfinished. They are never shippable final states. A final render rejects either one, as
well as every accepted delivery with no landing. Exit 2 means the page rendered but a beat
failed validation and carries an `UNPROVEN` chip. Read the authoritative beat with
`get-beat`, fix the complete object through `put-beat`, and re-render; do not ship an
unproven page. Publish with the `Artifact` tool. If that tool is unavailable, re-render
with `--standalone` so the file opens correctly in a browser, and report the local
`$R/report.html` path instead.

**Branch mode.** The commits already exist from Phase 3. Report the branch and
`git log --oneline`, and offer to push and open a PR. Do not do either unasked. A session
with no requested implementations leaves no branch at all, which is correct. For a PR
target, run `sessionctl.py check-pr "$R"` and `sessionctl.py check-worktree "$R" "$PWD"`
once more immediately before any requested push. For an open PR, push `HEAD` to the exact
frozen `head_repo` and `head_ref`, never to an inferred upstream. The push may
intentionally advance that PR after the check; any later work belongs in a replacement
session tied to the new head.

**Review mode.** Build the payload at `$R/review.json`, never inside the repo being
reviewed:

```json
{
  "body": "...",
  "commit_id": "<frozen target.head_sha>",
  "event": "COMMENT",
  "comments": [{"path": "src/auth/session.go", "line": 88, "side": "RIGHT", "body": "..."}]
}
```

The verdict, approve versus request changes, is the reviewer's. Ask for it. Validate
anchors before anything else, because GitHub rejects the whole review if one anchor is
outside the diff:

```bash
$S/scripts/validate-anchors.py --session "$R" --payload $R/review.json --out $R/review.fixed.json
```

Exit 2 means anchors were snapped or folded into the body. Report what moved in one line
and carry on; a bad anchor must never cost the session's work. Session mode reads and
hashes the same diff bytes it validates, requires the payload's full `commit_id` to match
the frozen head, requires a final `COMMENT`, `APPROVE`, or `REQUEST_CHANGES` event, and
adds the session's stable hidden delivery marker to the body. Show the final payload and
get one explicit yes. Recheck the open target after that yes,
immediately before the only GitHub side effect:

```bash
set -e
ACTOR=$(gh api user --jq .login)
$S/scripts/sessionctl.py check-pr "$R" --require-open
gh api repos/<owner>/<repo>/pulls/<n>/reviews --method POST \
  --input "$R/review.fixed.json" > "$R/review-response.json"
$S/scripts/sessionctl.py review-receipt "$R" "$R/review-response.json" \
  --actor "$ACTOR"
```

`review-receipt` requires exactly one non-pending review with the frozen commit, stable
delivery marker, and authenticated actor, then returns its URL. A transport failure or
5xx has an unknown outcome. Reconcile every page of reviews before retrying:

```bash
gh api --paginate --slurp repos/<owner>/<repo>/pulls/<n>/reviews \
  > "$R/review-candidates.json"
$S/scripts/sessionctl.py review-receipt "$R" "$R/review-candidates.json" \
  --actor "$ACTOR"
```

One exact match proves the delivery landed. More than one is a conflict. No match is not
permission to post immediately: allow for API visibility, repeat the read, then re-run the
final target check and obtain approval before a retry. Never match only `commit_id` and
never blindly post the review twice. The receipt includes the review state. `DISMISSED`
proves the post happened but is no longer an active delivery: preserve that receipt, stop
for supervised handling, and do not retry or mark the pending beats landed.

Only after a response or reconciliation proves that no review was created, use
`sessionctl.py reconcile "$R"` to list the pending `cause_seq` values, then record each
definite publication failure with `sessionctl.py fail "$R" <cause_seq> '<failure>'
'<owed>'`. The page will show Review publication failed while preserving the accepted
finding. Never mark an unknown outcome failed. A later verified post can still land the
same accepts.

A 422 here means the audience call was wrong upstream. Audience mode is frozen once an
accept is recorded, so stop and create a supervised replacement session with the correct
audience. Do not reclassify the pending delivery or downgrade the event to make the
command succeed.

Use the verified response's review URL. Write one land-entry JSON object with
`state: landed`, what the review delivered, and that URL in `where`. Run
`sessionctl.py reconcile "$R"` to obtain each pending accepted beat's `beat_n` and
`cause_seq`, then record the same URL for each:

```bash
$S/scripts/sessionctl.py land "$R" <cause_seq> <beat_n> <review-url> \
  --kind review --entry review-land.json
```

The shared entry is added to `lands[]` only once. Persist the outcome, review URL, and
`status` as one `patch-session` object. Once that observed external effect is recorded,
run `check-pr` again. Exit 2 now means the review was posted to the frozen commit but the
PR moved during submission. Report it as posted but stale and stop before any new effect;
never discard the durable landing receipt.

```bash
$S/scripts/sessionctl.py check-pr "$R"
```

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
session.sqlite3  authoritative versioned session, target identity, actions, and delivery
.session.lock    cross-process initialization, snapshot, and export lock
session.json     derived compatibility export of the session document
beats/01.json    derived compatibility export of one beat
decisions.jsonl  derived compatibility export of reviewer actions
ack.json         derived compatibility export of handled_seq
pr.json          GitHub PR metadata captured with the target
pr.diff          frozen local three-dot diff, hash-bound to the target
serve.json       running server URL and pid, removed when it exits
report.html      rendered, regenerable, throwaway
```

All mutations go through `sessionctl.py` or the server's `/act` endpoint. All reads used
for a later mutation go through `get-session` or `get-beat`. `session.json`, `beats/`,
`decisions.jsonl`, and `ack.json` are compatibility exports. The renderer reads SQLite
whenever it exists, and a later `$S/scripts/sessionctl.py export "$R"` repairs missing or
corrupt compatibility exports.
`check-pr` verifies the frozen diff and the current PR identity; an exact `snapshot-pr`
replay can repair `pr.diff` only while GitHub still names the same frozen target.

`target` is a write-once `{version, kind, repo, number, state, merged_at, base_sha,
head_sha, head_repo_id, head_repo, head_ref, merge_base_sha, changed_files, diff_sha256,
diff_bytes}` object. Generic session writes cannot add, remove, or alter it. A target must
be frozen before the first beat or action. A merged PR's top-level `delivery_branch` is
also write-once through `pin-branch` before its first local change.

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
reviewer answers. `accepted` is the durable state behind Implement and Include in review;
`decided` is the outcome behind Record decision. Set top-level `resolution_kind` to
`decision` only when the answer itself completes the work. New ordinary flags persist
`delivery`; imported legacy beats may omit it. `slots` accepts only the six keys from rule
2. `diff` is a list of raw lines, classified on the first character. `lands[]` entries are
`{state: landed|ready|open, what, where}`.

`landed` names what an accepted beat became: a commit SHA in `branch` mode, the review URL
in `review` mode, with `branch` beside it when there is one. A frozen PR records the full
SHA so the local-history guard can resolve it without ambiguity. An accepted beat may omit
it in a live render, where delivery remains visibly pending or failed. The `--final`
render rejects every accepted beat that still names nothing. A `decided` beat never
carries one; its `call` is what it became.

These accumulate into a review history. When a later session touches the same paths, read
the prior sessions for context.

## Formatting

- `file:line` references always, so every claim is checkable
- Quote code inline, short, only the lines that carry the point
- No emojis, no em-dashes, no preamble, no "great question"
- Say plainly when you are inferring rather than reading
