**Eval mode.** An argument carrying `--eval` is a scored replay, not a review:
`evals/run.py` drives it headlessly and nobody is at the keyboard. Every rule in `$S/SKILL.md` still
holds, the second read included. At each of the two decisions take the option you would
recommend and record it in your own words as the call, marked as eval mode. Do not serve
the page, post status, or park: write each beat and go straight to the next. Leave every
flag open, deliver nothing, skip Phase 4, and end with the recommendation and the count of
flags raised. The last write is one `patch-session` carrying the final `recommendation` and
`eval_complete: true`; the scorer counts a walk without that marker as unfinished, because
a provisional recommendation exists from Phase 1 onward. The runner allows writes only under `$UNDERWRITE_REVIEWS`, so
write each document for `sessionctl.py` to a file in `$R` and pass its path instead of
piping it.
