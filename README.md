# underwrite

Read a pull request closely enough to stand behind it. One beat at a time, driven by you,
ending in a delivered result.

Most review tooling hands you a wall of findings and leaves the work of deciding to you.
This walks a change in causal order, one coherent unit per turn, and stops after each one
so you steer. When something is worth flagging, the action names what happens next:
Implement applies and verifies a branch fix, Include in review queues a finding for the
GitHub review, and Include in report records a finding in the durable report.
Record decision stores an answer whose words complete the work.
Including a PR finding never authorizes execution. A separate implementation approval may
link one accepted finding to an attested child session that lands a local branch commit.

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
the PR description against what the diff actually does. This is the first of two decisions
before the page opens: confirm it, or correct either half, and your correction frames the
rest.

**Plan.** Every changed file is tiered into core, enabling, follow-through, and risk. The
second decision: walk it in that order, reorder it, skip a tier, or move a file that got
tiered wrong. Both decisions arrive as named options, not as a question you have to infer.

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
exists. A PR snapshot remains no-exec and cannot implement directly. After its finding has
been accepted and acknowledged, a separate approval may create a one-finding child with
branch audience and `gateway_attested` execution. The source session stays unchanged.

**Finish.** Local branch and working-tree targets may land requested implementations as
commits. An open PR lands accepted findings as one anchor-validated GitHub review. The
review carries the frozen full head as `commit_id`, so a PR update cannot silently move
delivery onto code that was never walked. A non-open PR has no open review delivery
target, so Include in report makes the accepted SQLite beat its terminal report outcome.
It does not call `land` or create a GitHub effect.
A linked child may land its verified output as one commit whose only parent is the frozen
PR head. It updates only its generated local branch and never pushes.

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

The host execution protocol is documented in
[`docs/host-execution-protocol.md`](docs/host-execution-protocol.md). Its
[`execution_receipt.py`](skills/underwrite/scripts/execution_receipt.py) module checks a
signed receipt contract plus caller-supplied bindings through an out-of-band verifier. It
does not provide signing keys or authenticate a host on its own.

The repository includes a standalone privileged host gateway in
[`gateway/`](gateway/README.md). Every source PR session still remains `no_exec` with its
frozen review or report audience. A separate implementation authorization binds one
acknowledged source accept, its exact beat revision, the frozen target, an actor, and the
actor's approval text. It creates a one-beat child with branch audience and
`gateway_attested` execution instead of changing the source session.
The gateway's one-shot [`adapter.py`](gateway/adapter.py) accepts that child's reserved
request and publishes the exact six-file evidence handoff consumed by the linked workflow.

The child accepts execution evidence only for its reserved request. It independently
checks the frozen source bundle, signed capability and receipt, output bundle, complete
streams, trusted job and sandbox profile, and expected exit code. Signatures must verify
under a pinned P-256 public key whose fingerprint matches that profile. A conforming
receipt or caller-supplied sandbox label alone remains evidence, not authorization.

Landing materializes the verified output, creates a local SHA-1 commit with the frozen PR
head as its sole parent, remeasures that commit against the signed SHA-256 output tree, and
compare-and-swap updates only the generated local branch. The target repository requires
Git 2.36 or newer, explicit object and ref durability, and exclusive controller access while
linking or landing. Session directories and Git metadata must be private local POSIX storage
that gateway jobs and untrusted concurrent writers cannot modify. The gateway must run in
its own supervised process because it installs process-wide host resource limits before
handling untrusted artifacts. Neither the gateway nor the linked implementation path pushes.

## The page drives

A walk serves itself on loopback, and the page is where you actually review. While a walk
is listening, the beat being read leads the page on its own, under a bar that stays in
view: the plan as a track with the current beat marked, the agent's own sentence about
what it is doing, and Next beat. Everything already walked sits below, ordered by what is
owed rather than by what was walked: open flags expanded at the top, resolved beats folded
to one line with your call on it, clean beats to one line each that still carries its
proof. When the walk ends, that ledger is the page, and it is what the final render keeps.
The keys do what the row under the beat says: Enter saves a note, or records a decision
when the note is the decision; ⌘Enter or Ctrl+Enter fires the row's primary action; `n`
is Next beat and `f` goes to the first open flag.
Drop is the one control with no undo, so it asks: the first click arms it and the second
one sends. Anything else answers no, including three seconds of nothing.

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
For a PR source, that `accept` delivers only to its frozen review or report audience. The
separate linked implementation authorization records its own actor and approval and never
reinterprets Include in review or Include in report as execution permission.

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

An optional linked implementation continues in a separate child:

```
accepted source finding + explicit implementation approval
                         -> one frozen one-beat child
reserved request         -> supervised gateway execution
six-file evidence set    -> pinned signature and artifact verification
verified output          -> exact local commit, compare-and-swap branch update
```

The source session keeps its original audience and `no_exec` policy throughout. A moved PR
head requires a replacement source snapshot; it never refreshes either session in place.

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
implementations/<link-id>/  one linked child, nested below the source session
  session.sqlite3           authoritative child, attempt, evidence, and landing state
  implementation-evidence/<attempt>/
    request.json            exact reserved gateway request
    capability.dsse.json    signed host capability
    receipt.dsse.json       signed execution receipt
    output.bundle           verified output transport
    stdout                  complete captured standard output
    stderr                  complete captured standard error
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
`diff`, `read-blob` and `context-log` are the argv-safe gateways for inspecting those
frozen objects without checking out the PR; each one verifies against the frozen digest
before it returns anything.
`check-pr` refuses a moved base, head, route, or lifecycle before an external effect, while
`check-controller` requires the clean controller to remain at the frozen base. The frozen
diff, trusted context, and object bundle are checked by byte count and SHA-256.
Controller cleanliness is rechecked immediately before capture freezes the target. It is
a point-in-time precondition, not an attestation against a concurrent local writer.

Linked creation is deterministic for one source action and refuses a conflicting second
authorization. A complete child is atomically published at its deterministic path, so a
crash cannot expose a partial final session. Request reservation reuses a nonfailed attempt;
a definite failed attempt gets a new attempt number and challenge. Evidence consumption and
landing accept identical retries but reject changed bytes. The prepared commit plan is
durable before the branch update, so a restart can observe an already-updated ref and finish
the SQLite receipt. A
moved target before the update blocks landing and requires a replacement source snapshot.
A move detected after the local update preserves the landed commit and reports that a
replacement is required, without causing another effect.

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
