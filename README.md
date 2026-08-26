# underwrite

Read a pull request closely enough to stand behind it. One beat at a time, driven by you,
ending in code.

Most review tooling hands you a wall of findings and leaves the work of deciding to you.
This walks a change in causal order, one coherent unit per turn, and stops after each one
so you steer. When something is worth flagging, the flag arrives with the patch already
written and run, so it is accept-or-drop in one word. Accepted flags become commits.

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

**Ingest.** Reads the PR, the diff, the last twenty commits touching those paths, and the
two or three earlier PRs the squash-merge subjects point at. That last one is not padding:
prior work in the same area is reliably where the best finding comes from, because it is
the context a diff cannot show you.

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

`PROOF` names a command that was run or a file that was read. `inferred` is a legal value.
A claim with neither does not ship.

**Land.** The medium is decided at ingest, from whether anyone will read it. A merged or
self-authored PR with no other reviewers lands as commits on a branch created lazily at
the first accepted flag, so a session with no accepts leaves no trace. Anything with a
real audience lands as a single GitHub review, anchor-validated first because one bad
anchor rejects the whole thing.

## The page drives

A walk serves itself on loopback, and the page is where you actually review. Beats stream
in as they are walked, ordered by what is owed rather than by what was walked: open flags
expanded at the top, accepted next, clean beats collapsed to one line each that still
carry their proof.

Decisions happen there too. An open flag carries Accept and Drop plus a field for putting
it in your own words, and Next beat advances the walk from anywhere. Clicking is what
unblocks the terminal side, which parks on the server between beats rather than spinning.
The terminal still takes the same answers in words, so closing the tab never strands a
session.

```
terminal                     browser
--------                     -------
presents beat 5    ------>   beat 5 appears, FLAG, expanded
(parked on /await)           [ accept ]  [ drop ]  [ your words ]
                   <------   POST /act
writes the patch
runs verification
commits 961eb58    ------>   beat 5 flips to ACCEPTED
presents beat 6    ------>   beat 6 arrives
```

The server is loopback-only and takes no path from any request: every read and write is a
fixed name inside the session directory. Loopback is not authentication on its own, so it
also refuses a request whose Host is not loopback, or whose Origin is not the page's own.

## Session state

Sessions live in `~/.claude/reviews/<owner>-<repo>-pr<N>/`, outside every repo, so a review
never shows up in `git status`. They are resumable, which matters because the large PRs
that most need underwriting are the ones nobody finishes in one sitting.

```
session.json     facts, audience, plan, status, cursor, what lands
beats/01.json    one file per beat, written as it is walked
pr.diff          the saved diff
report.html      rendered, regenerable
```

The three scripts are Python 3 stdlib, no dependencies. `serve.py` runs the walk.
`render-report.py` turns a session into the page and validates it on the way through: a
flag with no fix, a clean beat with no proof, a proof naming no command, or a beat you
accepted that landed nothing gets an `UNPROVEN` chip and exit 2. Review mode permits that
last state only in the preview before the GitHub POST; its final render requires the
review URL. The page still renders when validation fails. `validate-anchors.py` snaps
review comments to lines that exist in the diff and folds unsnappable ones into the body
rather than dropping them.

Iterating on the page design means editing `skills/underwrite/assets/report.css`, or
passing `--css` to try something without a commit.

## License

MIT
