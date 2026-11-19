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

`second_read` holds exactly `verdict` and `evidence`. `confirmed` and `unread` sit only on a
flag or what a flag became; `disputed` sits only on an `unverified` beat, the demoted flag.
The evidence is at most 25 words. Beats from before the field carry none.

A flag's `decision` is an object with exactly
two nonempty text fields, `question` and `recommendation`. These describe existing choices,
not new actions. Write both for every new review; older sessions without them stay readable
and visibly say the recommendation was not recorded. An accepted report freezes its
decision wording with the finding.

Question history is a read-only projection of ordered action receipts, separate from
`call` and finding text. Each entry has `seq`, `question`, `state`, `answer`, and
`evidence`; unanswered entries have no answer or evidence. Never write a beat's
`questions` by hand. Older sessions without question actions show no question history.

`$S/scripts/sessionctl.py stats ~/.claude/reviews` counts what every stored session raised
and how the reviewer answered it, reading each one from a backup copy. Run it before and
after a change to this skill: a change that raises the dropped share is a regression even
when it finds more.
