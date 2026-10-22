# Misses

A miss is a defect that shipped through a pull request and was found later. Each one here is
an eval case under `evals/cases/`, and each entry asks the question the Anthropic code review
write-up asks of every miss: what did the reviewer not have in front of it? A change to the
skill is kept only when `evals/run.py` still finds every miss without raising more false
alarms on the clean cases.

## #83 refused legacy sessions in `links`

- **Shipped:** `attested_implementation.py:561` refused any source without `session.sqlite3`.
  A legacy session is `session.json` plus `beats/` and no database, and its first open is the
  migration, so the guard refused the one shape that converts itself.
- **Found by:** #84, which moved the predicate into `SessionStore.__init__`, the one place
  every caller passes through.
- **What was missing:** which on-disk shapes count as a session was written down nowhere a
  reader of `links` would look. The predicate looked obviously right in isolation.
- **Case:** `underwrite-83-legacy-guard`. No walk of #83 was stored, so this enters as a case
  rather than a replayed session.

## #76 canonicalized slots on write and compared them raw

- **Shipped:** `session_store.py:1550` canonicalized incoming slot keys, while line 907 skipped
  the repair for pinned beats. A pinned beat read with `get-beat` and written straight back
  then counted as a change, deleted the `fix` slot and voided the authorization.
- **Found by:** #79, which compares the stored beat as the store spells it.
- **What was missing:** the invariant that a comparison reads both sides through the same
  spelling. The two halves sat 600 lines apart, and the diff showed each one as correct.
- **Case:** `underwrite-76-normalize-compare`.

## Candidates not admitted

- **cornermind #29:** `migrate.yml` allowed `workflow_dispatch` from any branch, later guarded
  in cornermind #54. The `production-db` environment already allowed only `main`, so the fix
  was defense in depth, not a shipped defect.
- **ai-kit #15:** the break-check gate could be bypassed by editing its own config, fixed in
  ai-kit #17. The PR body named the gap and deferred it, so a walk that stayed quiet was not
  wrong.

## Adding a miss

1. Write `evals/cases/<repo>-<number>-<slug>.json` with the PR's base and head, the defect's
   `path` and `lines` as they read at the head, and the fix as `evidence`. The lines must sit
   inside the PR's diff, or no walk can flag them.
2. Add an entry here naming what the reviewer did not have in front of it.
3. Run `python3 evals/run.py <case-id>` on the current skill and record the result.
