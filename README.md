# underwrite

Read a pull request closely enough to stand behind it. One beat at a time, driven by you,
ending in a delivered result.

Most review tooling hands you a wall of findings and leaves the work of deciding to you.
This walks a change in causal order, one coherent unit per turn, and stops after each one
so you steer. When something is worth flagging, the action names what happens next:
Implement applies and verifies a branch fix, Include in review queues a finding for the
GitHub review, and Include in report records a finding in the durable report.
Record decision stores an answer whose words complete the work.

## Install

```
/plugin marketplace add radkode/underwrite
/plugin install underwrite@underwrite
```

## Use

```
/underwrite 42          a PR number
/underwrite my-branch   a branch, diffed against main
/underwrite             the working tree
```

## How a session goes

**Ingest.** Freezes the PR's exact base and head, no-exec policy, and lifecycle-derived
audience in one transaction, fetches both revisions into a bare repository, and builds
their three-dot diff locally. Open PRs use review mode; every non-open PR, whether merged
or closed without merge, uses report mode. That avoids remote display limits and binds
every later check to full commit IDs. The same capture freezes governing instructions
from the base, separately from the untrusted head. It then reads the last twenty base
commits touching those paths and the two or three earlier PRs the squash-merge subjects
point at. That last one is not padding: prior work in the same area is reliably where the
best finding comes from, because it is the context a diff cannot show you.

**Orient.** A short reconstruction of what the change is for, plus a claim check comparing
the PR description against what the diff actually does. You confirm or correct it, and
your correction frames the rest.

**Plan.** Every changed file is tiered into core, enabling, follow-through, and risk. You
reorder or skip before anything is walked.

**Walk.** One beat per turn, opening with a verdict token and running on fixed lines:

```
BEAT 5/7  enabling  .github/workflows/ci.yml:22
FLAG   unpinned attw resolves latest on every CI run

WHAT   adds `attw --pack . --profile esm-only` between build and eval
PROOF  `npm view @arethetypeswrong/cli versions` shows every major is 0
RISK   a new rule reddens a PR that changed nothing
PRIOR  #2 pinned break-check to 0.6.0 citing this exact failure mode
FIX    npx --yes @arethetypeswrong/cli@0.18.5
```

`PROOF` names a command run under the session's execution policy, or a file that was read.
`inferred` is a legal value. A claim with neither does not ship.

`FIX` is the smallest concrete implementation intent, review recommendation, or decision
owed. For local branch and working-tree targets, choosing Implement authorizes Underwrite
to apply that intent and run verification. It does not claim that an exact patch already
exists. PR snapshots are no-exec and keep implementation unavailable.

**Finish.** Local branch and working-tree targets may land requested implementations as
commits. An open PR lands accepted findings as one anchor-validated GitHub review. The
review carries the frozen full head as `commit_id`, so a PR update cannot silently move
delivery onto code that was never walked. A non-open PR has no open review delivery
target, so Include in report makes the accepted SQLite beat its terminal report outcome.
It does not call `land` or create a GitHub effect.

## Trust and execution

Start a PR review from a clean checkout of its base, never from the PR head. Keep that
controller checkout on the base for the whole session. A controller may load repository
instructions before Underwrite begins, so discovering the mistake later is not enough:
stop and restart from the base checkout.

Every PR head is untrusted executable input, regardless of author, fork, audience, review,
or merge state. Underwrite reads the head, PR conversation, linked issues, and command
output as data. Governing repository instructions come only from the frozen base revision.
Changed `AGENTS.md`, `AGENTS.override.md`, `CLAUDE.md`, `CLAUDE.local.md`, contribution
guidance, and ADRs are reviewed like any other change; they do not govern their own review.

Every PR session freezes `no-exec` and its lifecycle-derived audience atomically with its
target. Underwrite may inspect the hash-bound diff and frozen Git objects through its
static readers, but it does not check out the head, install, build, test, lint, run repo
scripts or interpreters, or invoke Git hooks and filters. Review comments, report
inclusions, notes, navigation, and recorded decisions still work.

Underwrite does not currently create or attest a host sandbox, so it never accepts a
caller-supplied sandbox label as authorization. A future execution path needs a host-issued
receipt bound to the frozen target and verified tree. Until that boundary exists, all PR
code remains no-exec. Audience and execution policy are separate decisions.

## The page drives

A walk serves itself on loopback, and the page is where you actually review. Beats stream
in as they are walked, ordered by what is owed rather than by what was walked: open flags
expanded at the top, chosen resolutions next, clean beats collapsed to one line each that
still carry their proof.

Decisions happen there too. Review mode uses Include in review and Drop. Report mode uses
Include in report and Drop. Local branch and working-tree sessions retain Implement. A
policy question whose answer completes the work carries Record decision.
Each has a field for putting the call in your own words, and Next beat advances the walk
from anywhere. Clicking is what unblocks the terminal side, which parks on the server
between beats rather than spinning. The terminal still takes the same answers in words,
so closing the tab never strands a session.

The labels describe the delegated effect without changing the durable protocol. Implement,
Include in review, and Include in report all record the existing `accept` action. Record
decision uses the existing `decide` action.

A no-exec PR review flow looks like this:

```
terminal                     browser
--------                     -------
presents beat 5    ------>   beat 5 appears, FLAG, expanded
(parked on /await)           [ Include in review ]  [ Drop ]  [ your words ]
                   <------   POST /act with canonical accept and stable IDs
SQLite records the call, pending delivery, and application receipt
the frozen head remains unexecuted
presents beat 6    ------>   beat 6 arrives
final approval     ------>   one anchor-validated GitHub review is posted
records the landing ------>  accepted beats show the review URL
```

For a non-open PR, Include in report records `accept` and the accepted beat is complete in
the SQLite-backed report immediately. There is no pending delivery, `land` call, or GitHub
effect. `report.html` is a regenerable view of that durable state, not its delivery receipt.

The server is loopback-only and takes no path from any request: every read and write is a
fixed name inside the session directory. Loopback is not authentication on its own, so it
also refuses a request whose Host is not loopback, or whose Origin is not the page's own.
Each session has a durable identity, so a browser retry cannot land in another session if
an operating system later reuses the same loopback port.

## Session state

Sessions live in `~/.claude/reviews/<owner>-<repo>-pr<N>/`, outside every repo, so a review
never shows up in `git status`. They are resumable, which matters because the large PRs
that most need underwriting are the ones nobody finishes in one sitting.

```
session.sqlite3  authoritative versioned session, beats, action queue, and receipts
session.json     derived compatibility export
beats/01.json    derived compatibility export of one beat
decisions.jsonl  derived compatibility export of reviewer actions
ack.json         derived compatibility export of the handled cursor
pr.json          GitHub metadata captured with the target
pr.diff          frozen local three-dot diff, hash-bound to the target
pr.bundle        frozen base and head Git objects, hash-bound to the target
trusted-context.json  frozen base instructions, hash-bound to the target
report.html      rendered, regenerable
```

The scripts are Python 3 stdlib, with no dependencies. `serve.py` runs the walk and
`sessionctl.py` is the only command-line mutation path. SQLite commits a reviewer action
with its local beat change, deduplicates retries by action ID, and stores navigation as an
absolute application receipt. A crash before commit changes nothing; a crash after commit
resumes from the stored result without moving twice. Compatibility JSON is regenerated
from SQLite and is never authoritative once the database exists. The `snapshot-pr`
command in `sessionctl.py` records the write-once target, including the head repository
and ref, plus the trusted base context and Git object bundle. It records the no-exec policy
and derives the PR audience from the captured lifecycle in the same transaction. Existing
sessions keep their frozen audience; lifecycle drift requires a supervised replacement.
`read-blob` and `context-log` are the argv-safe gateways for inspecting those frozen
objects without checking out the PR.
`check-pr` refuses a moved base, head, route, or lifecycle before an external effect, while
`check-controller` requires the clean controller to remain at the frozen base. The frozen
diff, trusted context, and object bundle are checked by byte count and SHA-256.
Controller cleanliness is rechecked immediately before capture freezes the target. It is
a point-in-time precondition, not an attestation against a concurrent local writer.

`render-report.py` turns a session into the page and validates it on the way through. A
flag with no fix, a clean beat with no proof, or a proof naming no command gets an
`UNPROVEN` chip and exit 2. During a live walk, pending and failed delivery are visible
states and do not pretend to be completion: `Implementation pending` or `Implementation
failed` in branch mode, `Included, review pending` or `Review publication failed` in
review mode. A final render uses `--final` and rejects any chosen branch or review delivery
that has not landed. Report accepts are already terminal and require no landing. Review
mode omits that flag only for the preview before its GitHub POST. The page still renders
when validation fails.
`validate-anchors.py` snaps review comments to lines that exist in the diff and folds
unsnappable ones into the body rather than dropping them.

Iterating on the page design means editing `skills/underwrite/assets/report.css`, or
passing `--css` to try something without a commit.

## License

MIT
