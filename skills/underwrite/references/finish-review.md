For an outstanding review intent, follow the
Review mode guidance in this file. For a `prepared` or `submitting` intent, use `submit-review` to
verify, supervised `orphan-review` for a stored review that cannot land, or `retire-review`
before a supervised replacement when no review id is stored. For a `failed` or `retired`
intent, prepare and preview a replacement, then get explicit approval before submitting it.

**Review mode.** Write the review body to `$R/review-body.md` and per-beat comments to
`$R/review-comments.json`, never inside the repo being reviewed. Use `sessionctl.py
reconcile "$R"` to find the accepted beats still owed review delivery, including failed
ones. The comments object must have exactly one entry per such beat, keyed by beat number:

```json
{
  "1": {"path": "src/auth/session.go", "line": 88, "side": "RIGHT", "body": "..."}
}
```

The verdict, approve versus request changes, is the reviewer's. Give your evidence-backed
recommendation first, then ask for it. Use `COMMENT`, `APPROVE`, or `REQUEST_CHANGES` as
chosen. A pull request author cannot approve their own PR, so use `COMMENT` for self-review.
Prepare the store-owned intent:

```bash
$S/scripts/sessionctl.py prepare-review "$R" --event COMMENT \
  --body-file "$R/review-body.md" --comments-file "$R/review-comments.json"
```

Preparation binds the covered beats and full frozen head to immutable payload bytes,
validates anchors against the hash-verified diff, and adds the stable hidden delivery
marker. It writes `$R/review.fixed.json` and returns the intent id, event, covered beats,
anchor changes, and digest. Report snapped or folded anchors in one line. Show the preview:
event, covered beats, and the complete `$R/review.fixed.json` payload. Get one explicit yes
for that intent before submitting. If the reviewer changes it, prepare and preview again;
`--replace` can replace only a never-posted prepared intent with no stored review id.
With a stored id, use `submit-review` to verify or supervised `orphan-review`, not `--replace`.

```bash
$S/scripts/sessionctl.py submit-review "$R" --intent <intent-id>
```

`submit-review` refuses changed preview bytes, runs `check-pr --require-open`, and durably
marks the intent submitting before the POST, storing the posting actor in the same transition.
It verifies the frozen commit, marker, stored actor, event/state, the validated body including
folded findings, and inline comment content including multiline ranges. Only CRLF line endings
and trailing body whitespace are normalized. Reconciliation uses the stored actor even after
an account switch; a different current actor cannot POST (exit 1, naming both logins).
A verified receipt lands exactly the intent's beats and adds one shared `lands[]` entry, atomically. Beats
accepted later stay pending and need a separate approved intent. Never POST or land review
beats by hand.

Transport failures, 408, 429, 5xx, and 422 are uncertain. The command reconciles every page of
reviews: one verified match lands, more than one is a conflict. Exit 3 means a POST may exist;
the intent stays submitting or keeps its stored review id, not failed.
Repeat the same `submit-review` command to read again; it does not
POST again without `--retry`. Allow for API visibility and repeat the read before seeking
explicit approval for `--retry`; retry only after reconciliation finds no review. A retry
rechecks the open target. A stored review id is verified without reposting, even with
`--retry`. Never blindly post twice or infer a new audience from a 422.
`DISMISSED` records a receipt without landing: stop for supervised handling and do not
retry. Only an initial POST from a prepared intent can treat a definite other 4xx with no
match as publication failure for every covered beat, preserving the accepted findings.
A retry after an uncertain attempt stays submitting even on 4xx.
An unknown outcome must never be marked failed.

Exit 5 means could not check GitHub before posting: nothing posted, intent unchanged; rerun
`submit-review`. This is a prepared intent's pre-POST read failure, with no stored review id.
Exit 4 means the target moved before POST: this invocation did not post, intent unchanged.
For a submitting intent, an earlier POST may still surface. Stop and use
`sessionctl.py retire-review "$R" --intent <intent-id>` before a supervised replacement.
Retirement accepts a prepared intent, or a submitting one only after two zero-match
reconciliations under the submission lock, at least 30 seconds apart by default
(`--reconcile-delay` configures seconds); it refuses any stored review id.
A matching review lands with exit 0 and prints its receipt; follow the landing steps below.
For a stored review that cannot land, use supervised `sessionctl.py orphan-review "$R"
--intent <intent-id> --reason <text>`: records URL and reason, leaves beats pending, blocks repost.

After landing, persist the outcome, review URL, and `status` as one `patch-session` object.
Recheck the target after posting:

```bash
$S/scripts/sessionctl.py check-pr "$R"
```

Exit 2 now means posted but stale: report that the PR moved during submission, preserve
the durable receipt, and stop before any new effect. Do not retry the review. Then render
with final delivery checks:

```bash
$S/scripts/render-report.py $R --final --standalone
```

If write-back was interrupted, repeat `submit-review` for the same intent to reconcile or
replay its durable landing, not to create another review. Repair session metadata through
`patch-session` and render again. Pending or failed deliveries and unresolved review
intents, including ones with no findings, are not final. Follow the outstanding-review
recovery guidance above. Once it passes, report the review URL and updated local page. Publishing that page remains the
separate opt-in under **Publishing is opt-in.** in `$S/SKILL.md`. If the reviewer already agreed to publish an earlier render,
re-publish the updated artifact; otherwise make the publishing offer under **Publishing is opt-in.** in `$S/SKILL.md` now.
