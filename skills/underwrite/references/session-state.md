# Session state

```
session.sqlite3  authoritative versioned session, target identity, actions, and delivery
.session.lock    cross-process initialization, snapshot, and export lock
session.json     derived compatibility export of the session document
beats/01.json    derived compatibility export of one beat
decisions.jsonl  derived compatibility export of reviewer actions
ack.json         derived compatibility export of handled_seq
pr.json          GitHub PR metadata captured with the target
pr.diff          frozen local three-dot diff, hash-bound to the target
pr.bundle        frozen base and head Git objects, hash-bound to the target
trusted-context.json  frozen base instructions, hash-bound to the target
serve.json       running server URL and pid, removed when it exits
report.html      rendered, regenerable, throwaway
report.partial.html  the page as it stands, rewritten by the server on every change
findings.md      derived export of the accepted findings, report audience only
implementations/<link-id>/  one source-bound implementation child
  session.sqlite3           authoritative attempt, evidence, plan, and landing state
  implementation-evidence/<attempt>/
    request.json            exact reserved request
    capability.dsse.json    signed host capability
    receipt.dsse.json       signed execution receipt
    output.bundle           verified output transport
    stdout                  complete captured standard output
    stderr                  complete captured standard error
proof-evidence/<proof>/     one verified attested proof
  request.json              exact reserved request
  capability.dsse.json      signed host capability
  receipt.dsse.json         signed execution receipt
  stdout                    complete captured standard output
  stderr                    complete captured standard error
```

`target` is a write-once `{version, kind, repo, number, state, merged_at, base_sha,
head_sha, head_repo_id, head_repo, head_ref, merge_base_sha, changed_files, diff_sha256,
diff_bytes, trusted_context_sha256, trusted_context_bytes, object_bundle_sha256,
object_bundle_bytes}` object. Generic session writes cannot add, remove, or alter it. The
target, top-level `no_exec` policy, and lifecycle-derived PR audience are frozen together
before the first beat or action. The policy always treats PR input as untrusted and binds
that mode to the target identity and digests. Older PR sessions without the current frozen
context require a supervised replacement.

An unversioned legacy action log with no `ack.json` imports as handled because replaying
it could duplicate a commit. A cursor-aware log with a missing, corrupt, or impossible
ack refuses automatic migration. Inspect the log and external effects, then run
`$S/scripts/sessionctl.py init "$R" --handled-seq <known-boundary>`. Never guess that
boundary.

If an old action's absolute effect is already visible, use `reconcile-action` with an
envelope containing `result`, the observed full session or beat documents, and nonempty
`evidence`, then run `$S/scripts/sessionctl.py reconcile-action "$R" <seq> recovery.json`.
If an action cannot be recovered, run `$S/scripts/sessionctl.py abandon-head "$R" <seq>
--actor <name> --reason '<why>'`. It only accepts the exact head and never skips an action
silently.

`state` is one of `clean`, `flag`, `unverified`, `accepted`, `dropped`, `decided`. The
first three are what a beat opens with; the last three are what a flag becomes after the
reviewer answers. `accepted` is the durable state behind Implement, Include in review, and
Include in report; `decided` is the outcome behind Record decision. A `dropped` beat
written now carries `reason`, one of `wrong`, `intentional`, `not-worth-it`, `pre-existing`;
a beat dropped before the field existed has none. Set top-level
`resolution_kind` to `decision` only when the answer itself completes the work. New
ordinary flags persist `delivery`; imported legacy beats may omit it. `slots` accepts only
the six keys from rule 2, spelled lower case: `what`, `why`, `proof`, `risk`, `prior`,
`fix`. Rule 2 shows how the page labels them, not how the store spells them. `diff` is a list of raw lines, classified on the first character.
`proof_kind` is `ran`, `read`, `inferred`, or `attested` on every beat written now, and
`clean` requires `ran` or `attested`. `attested` requires `attested_proof`, the number of
a verified proof run for that beat, and the store owns `attestation`. A beat stored before the field existed keeps the verdict it landed with.
`lands[]` entries are `{state: landed|ready|open, what, where}`.

`second_read` holds exactly `verdict` and `evidence`. `confirmed` and `unread` sit only on a
flag or what a flag became; `disputed` sits only on an `unverified` beat, the demoted flag.
The evidence is at most 25 words. Beats from before the field carry none.

`recommendation` is nonempty session text. A flag's `decision` is an object with exactly
two nonempty text fields, `question` and `recommendation`. These describe existing choices,
not new actions. Write both for every new review; older sessions without them stay readable
and visibly say the recommendation was not recorded. An accepted report freezes its
decision wording with the finding.

Question history is a read-only projection of ordered action receipts, separate from
`call` and finding text. Each entry has `seq`, `question`, `state`, `answer`, and
`evidence`; unanswered entries have no answer or evidence. Never write a beat's
`questions` by hand. Older sessions without question actions show no question history.

`landed` names what an accepted beat became: a commit SHA in `branch` mode, the review URL
in `review` mode, with `branch` beside it when there is one. A frozen PR records the full
SHA so the local-history guard can resolve it without ambiguity. An accepted beat may omit
it in a live branch or review render, where delivery remains visibly pending or failed.
The `--final` render rejects every such accepted beat that still names nothing. Report
mode is different: acceptance itself is the terminal durable outcome, with delivery state
`none`, no `landed` value, and no `lands[]` entry. A `decided` beat never carries one; its
`call` is what it became.

`$S/scripts/sessionctl.py stats ~/.claude/reviews` counts what every stored session raised
and how the reviewer answered it, reading each one from a backup copy. Run it before and
after a change to this skill: a change that raises the dropped share is a regression even
when it finds more.

Sessions accumulate into a review history, and `history` is how a later session reads it:
for every other session on the same repository, its facts, its recommendation, and the
findings it left on the paths this one touches. Where the filesystem records when a store
was created, only earlier sessions appear, marked `order: earlier`; elsewhere every other
session appears marked `order: unknown`, so weigh it as context, not as the record. Sessions
it cannot read are listed as unreadable, never skipped silently.
