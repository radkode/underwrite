# Reviewing underwrite

Owner guidance for anyone, person or agent, reviewing a change to this repository.
Underwrite freezes this file from the base revision and reads it during the claim check
and the walk plan.

## What failure looks like here

- **The store accepts what it should refuse.** `session_store.py` owns every rule about
  beats, states, actions and the frozen target. A check that lives in `serve.py`,
  `render-report.py` or the skill prose instead of the store is a check the next caller
  skips.
- **The frozen target drifts.** Anything that reads `pr.diff`, `pr.bundle` or
  `trusted-context.json` outside the store's hash verification, or that lets head
  content act as policy, breaks the one promise the tool makes.
- **The skill and the scripts disagree.** `SKILL.md` names a command, a field or a rule
  the scripts do not implement, or the other way round. `tests/test_skill.py` pins the
  wording for a reason.
- **The page says less than the store knows.** A new state or field with no rendering,
  or wording a cold reader cannot act on. Judge the page by whether a reader who did not
  write it understands what to do next, not by how it looks.
- **A test that proves the implementation to itself.** Expected values derived from the
  function under test rather than from the skill, the README or an independent reference.
- **A looser gateway.** Any change under `gateway/` that lets a request run without an
  attested capability, a verified receipt or a valid signature.

## Paths not worth walking

- `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` version bumps: one
  beat, and the only question is whether both say the same version.
- Test fixture plumbing that only gives an existing fixture the shape a new rule requires
  (a real hunk on a line a test already flagged, for example), when the rule itself is
  walked elsewhere.

## What counts as evidence

- A named unittest run: `python -m unittest discover -s tests -p '<file>'`, and the CI
  matrix (Python 3.9 and 3.14) green on the exact head under review.
- Stdlib only. A new third-party import fails the 3.9 job and the plugin's install story.
- For anything the page shows, a cold read of a rendered session, not a screenshot
  judged for design.
- For skill or README prose, the `test_skill.py` assertion that pins it.
- No runtime claim from a static read. The review is `no-exec`; say what was read.
