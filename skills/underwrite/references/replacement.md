**A replacement walks only what moved.** Reconcile the old session and finish whatever
delivery it still owes. If the old session's audience is `review`, read `$S/references/finish-review.md` before finishing its delivery or any recovery. Then build the replacement beside it, never in it: with `$P` the
old session, the new `$R` is `$P` with `-r<k>` appended, `k` the next free round. Initialize
and snapshot it as in **Phase 0: scope and ingest** in `$S/SKILL.md`, and link it before the first beat:

```bash
$S/scripts/sessionctl.py link-predecessor "$R" "$P"
```

The link is written once. It records the round, the lines this PR's diff touches whose
content changed since the walked head (`moved`), and every flag from any earlier round with
its state and call (`carried`). A pure deletion counts the head lines on either side of it;
a binary or mode-only change moves the whole file, listed with no lines. Report it as one more ingest line, such as
`round 2   3 lines moved in 2 files, 4 findings carried`.

**A replacement plans only what moved.** Tier only the files in `predecessor.moved`, and
plan beats over those lines alone. Everything else was walked last round: say so in the
plan with the count, and the page carries the settled findings with their calls. An
accepted finding that landed in an earlier round already reached that round's review, so it
is not included again. A flag the earlier round left open, or accepted without delivering,
is walked again wherever it sits, because nobody settled it.

`over_drop` is optional text of at most 25 words on a flag whose line carries an earlier
round's drop; the store refuses it anywhere else.

A replacement lives at `$P-r<k>/` beside the session it replaces, and its write-once
`predecessor` holds `{session, round, base_sha, head_sha, moved, carried}`: `moved` maps
each path to the head lines that changed since `head_sha`, and `carried` lists every
flag from earlier rounds with its round, state, call, reason, and the words on its line.
A later round's call on the same line replaces an earlier one, except that a flag raised
`over_drop` leaves the drop it overrode in place. An open or undelivered finding is never
dropped from `carried`; once its line no longer reads the same it gains `line_changed`, the
round whose head changed it, and the page says so without claiming anyone looked again. Only `link-predecessor` writes it.
