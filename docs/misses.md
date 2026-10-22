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

## Runs

| Date | Skill | Case | Result | Cost |
|---|---|---|---|---|
| 2026-10-21 | second read demotes on any no | `underwrite-17-sigterm` | fail: one confirmed flag on a weak race test | $1.64 |
| 2026-10-21 | second read demotes on any no | `underwrite-83-legacy-guard` | fail: the walk flagged `:561`, the second read demoted it as not worth stopping | $1.89 |
| 2026-10-21 | demotes only on not introduced or not real | `underwrite-83-legacy-guard` | pass: `:561` flagged and kept, worth doubt noted in its evidence | $2.20 |

| 2026-10-21 | demotes only on not introduced or not real, before the completion marker | all five | 2 of 5 pass, 1 miss, 2 false alarms | $7.74 |
| 2026-10-21 | final skill, baseline in `evals/baseline.json` | all five | 3 of 5 pass, 1 miss, 2 false alarms | $9.24 |

In the baseline run `underwrite-83-legacy-guard`, `underwrite-12-host-guard` and
`ai-kit-5-core-deps` pass. `underwrite-76-normalize-compare` raised the right class of finding,
an upper-case slot read from before the repair, but at `session_store.py:2485`, far from the
defect at `:907` and `:1550`, so it scores as one miss and one false alarm; it is the live
miss for the next skill change to win back. `underwrite-17-sigterm` raised the same weak race
test flag on every run. `ai-kit-5-core-deps` raised an alarm in the run before and none in
this one; that alarm, that the guard never checks it parsed a dynamic import, is the class of
finding the original reviewer accepted in the stored session of that PR, so the case may be
mislabeled clean, and it stays until someone decides.

Walks are not deterministic: the same skill moved `ai-kit-5-core-deps` between pass and fail
across two runs. The baseline is one run, so a lone flip on a regression check is a reason to
rerun that case before believing it. Four walks of one run first stopped early because
exploratory commands were denied, which is why the runner now tells the walk what it may run.

The second run in the table is why the second read no longer demotes on worth. A replay of the six flags
stored before the second read existed agreed: it disputed two on worth, and the reviewer had
acted on one of them. The `underwrite-17-sigterm` flag is a fair question about a test that
catches the old bug only about one run in seven; it stands as a false alarm because nothing
later had to fix #17.

## Adding a miss

1. Write `evals/cases/<repo>-<number>-<slug>.json` with the PR's base and head, the defect's
   `path` and `lines` as they read at the head, and the fix as `evidence`. The lines must sit
   inside the PR's diff, or no walk can flag them.
2. Add an entry here naming what the reviewer did not have in front of it.
3. Run `python3 evals/run.py <case-id>` on the current skill and record the result.
