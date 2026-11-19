#!/usr/bin/env python3
"""Contract tests for the prompt-owned parts of the underwrite workflow."""
import json
import re
import unittest
from pathlib import Path

if __package__:
    from .skill_support import SKILL, skill_references, skill_text
else:
    from skill_support import SKILL, skill_references, skill_text

README = Path(__file__).resolve().parent.parent / "README.md"
MANIFEST = Path(__file__).resolve().parent.parent / ".claude-plugin"


class SkillCorpus(unittest.TestCase):
    def test_every_reference_is_linked_from_the_core(self):
        core = skill_text("SKILL.md")
        references = skill_references()
        self.assertTrue(references)
        for path in references:
            with self.subTest(reference=path.name):
                self.assertIn(f"$S/references/{path.name}", core)

    def test_every_core_reference_link_resolves(self):
        links = re.findall(r"\$S/(references/[^`\s]+\.md)", skill_text("SKILL.md"))
        self.assertTrue(links)
        for link in links:
            with self.subTest(link=link):
                self.assertTrue((SKILL.parent / link).is_file())

    def test_the_core_stays_within_its_line_budget(self):
        self.assertLessEqual(len(skill_text("SKILL.md").splitlines()), 950)

    def test_the_frontmatter_is_unchanged(self):
        expected = (
            "---\n"
            "name: underwrite\n"
            "description: Interactive PR review, one beat at a time. Reconstructs what a change "
            "is for and how it fits the project, walks it in causal order while the reviewer "
            "steers, resolves each flag at the moment it is raised, and finishes the chosen "
            "resolutions as commits, recorded decisions, durable report inclusions, or a "
            "GitHub review, whichever has a reader. Use for reviewing a PR or an unfamiliar "
            "diff, especially AI-authored changes where no author is around to answer questions.\n"
            "disable-model-invocation: true\n"
            "argument-hint: [pr-number | branch] [--eval]\n"
            "---\n"
        )
        self.assertEqual(skill_text("SKILL.md").split("\n\n", 1)[0] + "\n", expected)


class SkillReferenceTriggers(unittest.TestCase):
    def section(self, heading):
        return skill_text("SKILL.md").split(heading, 1)[1].split("\n## ", 1)[0]

    def test_eval_is_read_before_the_six_rules(self):
        opening = skill_text("SKILL.md").split("## The six rules", 1)[0]
        self.assertIn(
            "If the argument carries `--eval`, read `$S/references/eval-mode.md` first.",
            opening,
        )
        eval_mode = " ".join(skill_text("references/eval-mode.md").split())
        self.assertIn("Every rule in `$S/SKILL.md` still holds, the second read included", eval_mode)

    def test_the_proof_decision_precedes_its_procedure_pointer_in_phase_three(self):
        walk = self.section("## Phase 3: walk")
        decision = "A claim a single run would settle should be run, not read."
        unavailable = "When `$G` is not set up, there is no proof gateway: write `read` or `inferred`"
        pointer = "Before running it, read `$S/references/attested-proof.md`."
        self.assertIn(decision, walk)
        self.assertIn(unavailable, walk)
        self.assertIn(pointer, walk)
        self.assertLess(walk.index(decision), walk.index(unavailable))
        self.assertLess(walk.index(unavailable), walk.index(pointer))
        self.assertLess(walk.index(pointer), walk.index("**Read the comments"))
        self.assertLess(walk.index(pointer), walk.index("**Persist the beat"))
        procedure = " ".join(skill_text("references/attested-proof.md").split())
        self.assertIn('proofctl.py prove "$R" <beat> "$G"', procedure)
        self.assertIn("It exits 2 with `status: failed`", procedure)
        self.assertIn("It exits 3 with `status: retry` on an unknown outcome; repeat the same call", procedure)

    def test_linked_implementation_is_read_in_phase_three_before_decision_only_flags(self):
        walk = self.section("## Phase 3: walk")
        pointer = "Before creating or driving a linked PR implementation, read `$S/references/linked-implementation.md`."
        self.assertIn(pointer, walk)
        self.assertLess(walk.index(pointer), walk.index("When the flag itself is a decision"))

    def test_review_finish_is_read_at_phase_four_start_before_render_or_handoff(self):
        finish = self.section("## Phase 4: finish")
        pointer = "If the audience is `review`, read `$S/references/finish-review.md` now, before rendering."
        self.assertEqual(finish.strip().split("\n\n", 1)[0], pointer)
        for later in ("Refresh `recommendation`", "handoff", "render-report.py", "**Publishing is opt-in.**"):
            with self.subTest(later=later):
                self.assertLess(finish.index(pointer), finish.index(later))
        self.assertEqual(finish.count("$S/references/finish-review.md"), 1)

    def test_session_detail_and_outstanding_intent_pointers_are_at_startup_reconcile(self):
        session = self.section("## Session state")
        reconcile = next(
            paragraph for paragraph in session.split("\n\n")
            if 'sessionctl.py reconcile "$R"' in paragraph
        )
        self.assertIn("At startup and after any interrupted external effect, run", reconcile)
        self.assertIn(
            "Before migrating a legacy log, "
            "reading question history, using `stats`, inspecting the "
            "on-disk tree, or changing this skill, read `$S/references/session-state.md`.",
            reconcile,
        )
        self.assertIn(
            "If reconcile or the session shows an outstanding review intent, "
            "read `$S/references/finish-review.md` before any recovery.",
            reconcile,
        )
        self.assertEqual(skill_text("SKILL.md").count("$S/references/session-state.md"), 1)
        self.assertNotIn("Before reading or writing session or beat fields", skill_text("SKILL.md"))

    def test_action_recovery_rules_immediately_follow_startup_reconcile(self):
        session = self.section("## Session state")
        self.assertIn(
            '`$S/scripts/sessionctl.py reconcile "$R"`.\n'
            "If an old action's absolute effect is already visible, use `reconcile-action` with an\n"
            "envelope containing `result`, the observed full session or beat documents, and nonempty\n"
            '`evidence`, then run `$S/scripts/sessionctl.py reconcile-action "$R" <seq> recovery.json`.\n'
            'If an action cannot be recovered, run `$S/scripts/sessionctl.py abandon-head "$R" <seq>\n'
            "--actor <name> --reason '<why>'`. It only accepts the exact head and never skips an action\n"
            "silently.",
            session,
        )

    def test_supervised_replacement_pointers_precede_ingest_and_plan_continuation(self):
        ingest = self.section("## Phase 0: scope and ingest")
        pointer = "Before starting a supervised replacement, read `$S/references/replacement.md`."
        self.assertIn(pointer, ingest)
        self.assertLess(ingest.index("until it is understood."), ingest.index(pointer))
        self.assertLess(ingest.index(pointer), ingest.index("Otherwise say what is about to happen"))
        plan = self.section("## Phase 2: plan the walk")
        pointer = "Before planning a supervised replacement walk, read `$S/references/replacement.md`."
        self.assertIn(pointer, plan)
        self.assertLess(plan.index("**Move a file to another tier**"), plan.index(pointer))
        self.assertLess(plan.index(pointer), plan.index("This is a ten second interaction"))

    def test_replacement_reads_review_recovery_before_building_the_next_session(self):
        opening = skill_text("references/replacement.md").split("\n\n", 1)[0]
        pointer = (
            "If the old session's audience is `review`, "
            "read `$S/references/finish-review.md` before finishing its delivery or any recovery."
        )
        self.assertIn("finish whatever\ndelivery it still owes", opening)
        self.assertIn(pointer, opening)
        self.assertLess(opening.index(pointer), opening.index("Then build the replacement"))

    def test_small_finish_modes_stay_in_the_core(self):
        finish = " ".join(self.section("## Phase 4: finish").split())
        for rule in (
            "**Branch mode.** The commits already exist from Phase 3.",
            "Report the branch and `git log --oneline`, and offer to push and open a PR. Do not do either unasked.",
            "Report its generated local branch and exact commit, but do not push or open a PR as part of that flow.",
            "Any later publication is a new operation with its own explicit authorization.",
            "**Report mode.** Run the final render after the walk.",
            "Report mode does not create a GitHub effect and does not add a `lands[]` entry.",
            "`report.html` is a regenerable projection of the authoritative store, not a receipt and not a delivery target.",
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, finish)
        for name in ("finish-branch.md", "finish-report.md"):
            with self.subTest(reference=name):
                self.assertFalse((SKILL.parent / "references" / name).exists())
                self.assertNotIn(name, skill_text("SKILL.md"))


class CoreSessionFields(unittest.TestCase):
    def session(self):
        return " ".join(skill_text("SKILL.md").split("## Session state", 1)[1].split("## Formatting", 1)[0].split())

    def test_state_slots_and_raw_diff_format_are_in_the_core(self):
        session = self.session()
        for rule in (
            "`state` is one of `clean`, `flag`, `unverified`, `accepted`, `dropped`, `decided`",
            "`slots` accepts only the six keys from rule 2, spelled lower case: `what`, `why`, `proof`, `risk`, `prior`, `fix`",
            "Rule 2 shows how the page labels them, not how the store spells them",
            "`diff` is a list of raw lines, classified on the first character",
            "`proof_kind` is `ran`, `read`, `inferred`, or `attested` on every beat written now",
            "`clean` requires `ran` or `attested`",
            "`attested` requires `attested_proof`, the number of a verified proof run for that beat",
            "`recommendation` is nonempty session text",
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, session)

    def test_landing_basics_are_in_the_core(self):
        session = self.session()
        for rule in (
            "`lands[]` entries are `{state: landed|ready|open, what, where}`",
            "`landed` names what an accepted beat became: a commit SHA in `branch` mode, the review URL in `review` mode",
            "A frozen PR records the full SHA",
            "The `--final` render rejects every such accepted beat that still names nothing",
            "Report mode is different: acceptance itself is the terminal durable outcome, with delivery state `none`, no `landed` value, and no `lands[]` entry",
            "A `decided` beat never carries one; its `call` is what it became",
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, session)

    def test_history_ordering_is_next_to_the_phase_zero_history_read(self):
        ingest = skill_text("SKILL.md").split("## Phase 0: scope and ingest", 1)[1].split("## Phase 1", 1)[0]
        history = " ".join(ingest.split('- `$S/scripts/sessionctl.py history "$R"`', 1)[1].split("Run `check-pr`", 1)[0].split())
        for rule in (
            "`history` is how a later session reads it",
            "only earlier sessions appear, marked `order: earlier`",
            "elsewhere every other session appears marked `order: unknown`, so weigh it as context, not as the record",
            "Sessions it cannot read are listed as unreadable, never skipped silently",
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, history)


class SkillEvalGate(unittest.TestCase):
    def test_reference_only_changes_trigger_the_eval_workflow(self):
        workflow = (SKILL.parents[2] / ".github" / "workflows" / "evals.yml").read_text(encoding="utf-8")
        paths = workflow.split("    paths:\n", 1)[1].split("\npermissions:", 1)[0]
        for path in ("skills/underwrite/SKILL.md", "skills/underwrite/references/**", "evals/**"):
            with self.subTest(path=path):
                self.assertIn(f'      - "{path}"', paths)

    def test_docs_treat_the_references_as_skill_rules(self):
        readme = " ".join(README.read_text(encoding="utf-8").split())
        self.assertIn("change to the skill (`SKILL.md` or `references/`) only when it passes that", readme)
        self.assertIn("pull requests that touch the skill (`SKILL.md` or `references/`) or the evals", readme)
        review = (SKILL.parents[2] / "REVIEW.md").read_text(encoding="utf-8")
        self.assertIn("The skill (`SKILL.md` or `references/`) names", review)


class PublishedVersion(unittest.TestCase):
    """`claude plugin update` compares version strings, so a bump that lands in one
    manifest and not the other is a release that reaches nobody. It has happened three
    times: DD-1602 at 0.3.1, DD-2058 at 0.4.0, DD-2359 at 0.5.0."""

    def test_both_manifests_publish_the_same_version(self):
        plugin = json.loads((MANIFEST / "plugin.json").read_text(encoding="utf-8"))
        marketplace = json.loads((MANIFEST / "marketplace.json").read_text(encoding="utf-8"))
        entries = [p for p in marketplace["plugins"] if p["name"] == plugin["name"]]
        self.assertEqual(len(entries), 1, "marketplace must list this plugin exactly once")
        self.assertEqual(entries[0]["version"], plugin["version"])
        self.assertRegex(plugin["version"], r"^\d+\.\d+\.\d+$")


class BeforeThePageOpens(unittest.TestCase):
    """Phase 3 serves the page, so orient and plan are terminal-only. They were the two
    decisions with no named options, which made the reviewer guess what correcting even
    meant at the two moments that frame the whole session."""

    def skill(self):
        """Whitespace-collapsed: these are prose assertions, and a reflow that rewraps a
        sentence is not a behaviour change."""
        return " ".join(skill_text().split())

    def test_ingest_reports_progress_instead_of_going_quiet(self):
        text = self.skill()
        self.assertIn("never invent progress", text)
        self.assertIn("silence through it reads as a hang", text)

    def test_both_pre_page_decisions_name_their_options(self):
        text = self.skill()
        for option in (
            "**Confirm, and plan the walk**",
            "**Correct the reconstruction**",
            "**Correct the claim check**",
            "**Walk it in this order**",
            "**Reorder the beats**",
            "**Skip a tier**",
            "**Move a file to another tier**",
        ):
            self.assertIn(option, text)

    def test_both_answers_are_recorded_with_what_they_answered(self):
        text = self.skill()
        for key in ("`reconstruction`", "`claim_check`", "`orient_call`", "`plan_call`"):
            self.assertIn(key, text)
        self.assertIn("with the reviewer's answer in their own words", text)

    def test_the_options_are_offered_through_a_selectable_control(self):
        text = self.skill()
        self.assertIn("structured question tool", text)
        # and still works where the harness has no such tool
        self.assertIn("where no such tool exists", text)

    def test_working_tree_target_includes_staged_changes_and_never_substitutes_head_tilde(self):
        text = " ".join(skill_text("SKILL.md").split())
        ingest = text.split("## Phase 0: scope and ingest")[1].split("## Phase 1")[0]
        self.assertIn("`git diff HEAD`", ingest)
        self.assertIn("staged and unstaged", ingest)
        self.assertIn("untracked files are excluded", ingest)
        self.assertIn("`git diff HEAD~1`", ingest)
        self.assertIn("never substitute it automatically", ingest)
        self.assertNotIn("falling back to `git diff HEAD~1`", ingest)


class RecommendationContract(unittest.TestCase):
    def test_assessment_and_decisions_are_explicit_in_both_surfaces(self):
        text = " ".join(skill_text().split())
        for phrase in (
            "Lead the terminal with your assessment",
            "decision: {question, recommendation}",
            "names the choice and its options",
            "names the preferred option and why",
            "same assessment and count of decisions owed as the page",
            "agent's recommendation, not the reviewer's GitHub verdict",
            "A static review cannot claim verified runtime behavior",
            "Before the walk is complete, call it provisional",
            "say in the recommendation whether it blocks the merge",
        ):
            self.assertIn(phrase, text)


class TheHeadIsNotYourFinding(unittest.TestCase):
    """Rule 5 kept the head's prose out of the instructions and let it straight into the
    findings. On a well-commented self-authored PR that is most of a beat: three of six
    slots on dailydeck#1499 were the author's own docstrings, reworded."""

    def skill(self):
        return " ".join(skill_text().split())

    def test_rule_five_covers_laundering_and_not_only_obedience(self):
        text = self.skill()
        self.assertIn("never your finding either", text)
        self.assertIn("prose you must not launder", text)

    def test_it_says_what_to_do_instead_of_paraphrasing(self):
        text = self.skill()
        self.assertIn("what it adds that the diff does not already state", text)
        self.assertIn("quote the comment in the beat's lines and delete the slot", text)

    def test_the_self_authored_case_is_called_out(self):
        self.assertIn(
            "everything you are worth is in what they did not", self.skill()
        )

    def test_the_failure_is_shown_where_beats_get_written(self):
        text = self.skill()
        # a rule stated once in the preamble loses to the exemplars in Phase 3
        self.assertIn("Read the comments, then write what they do not say", text)
        self.assertIn("a slot you could have written without leaving the docstring", text)


class CleanMeansChecked(unittest.TestCase):
    """Across 22 real beats, 15 proofs were a file read and none opened UNVERIFIED, so
    CLEAN told the reviewer nothing about where to look. The store now refuses the
    spelling, and the walk only parks where the verdict asks something."""

    def skill(self):
        return " ".join(skill_text().split())

    def readme(self):
        return " ".join(README.read_text(encoding="utf-8").split())

    def test_only_a_proof_that_ran_earns_clean(self):
        text = self.skill()
        self.assertIn("Only `ran` and `attested` earn `CLEAN`", text)
        self.assertIn("`UNVERIFIED` is not a failure", text)
        self.assertIn(
            "`proof_kind` is `ran`, `read`, `inferred`, or `attested` on every beat written now",
            text,
        )

    def test_an_attested_proof_is_a_signed_prediction_the_store_holds(self):
        text = self.skill()
        self.assertIn("It signs a receipt only when the command exits with the code you predicted", text)
        self.assertIn("$S/scripts/proofctl.py prove \"$R\" <beat> \"$G\" --exit-code 1 --", text)
        self.assertIn("Never write `attestation`", text)
        self.assertIn("A wrong prediction is information, not evidence", text)
        self.assertIn("An exit code is coarse", text)
        self.assertIn("cite it only when that output shows your claim", text)
        self.assertIn("never `ran` for something you did not run", text)
        self.assertIn("this machine never executes the head", text)
        readme = self.readme()
        self.assertIn("A proof never changes the session's `no_exec` policy", readme)

    def test_a_risk_is_a_flag_or_nothing(self):
        self.assertIn("A `RISK` belongs to a flag", self.skill())
        self.assertIn("A risk appears only on a flag", self.readme())

    def test_the_walk_parks_only_where_the_verdict_asks(self):
        text = self.skill()
        self.assertIn("A `FLAG` beat stops and waits for the reviewer", text)
        self.assertIn("`CLEAN` and `UNVERIFIED` beats do not", text)
        self.assertIn("Moving past a clean or unverified beat", text)
        self.assertIn("`seq` above `handled_seq` means an action is queued", text)
        self.assertNotIn("One beat per turn", text)
        self.assertNotIn("One beat per turn", self.readme())
        self.assertIn("stops only where it needs a decision from you: a flag", self.readme())
        self.assertIn("land on the page and the walk moves on", self.readme())
        # the older instructions that parked after every beat, found by the #100 review
        self.assertNotIn("present the beat, then park.", text)
        self.assertNotIn("Advance only when the reviewer asks to move on.", text)
        self.assertNotIn("parks on the server between beats", self.readme())
        self.assertIn("past a `CLEAN` or `UNVERIFIED` beat, advance on your own", text)
        # a read-only beat asks for reading, not a decision, so nothing parks on it
        self.assertNotIn("`FLAG` or `UNVERIFIED` beat stops", text)
        self.assertNotIn("park on a `FLAG` or `UNVERIFIED`", text)
        self.assertNotIn("Past a `FLAG` or `UNVERIFIED` beat", text)
        self.assertNotIn("every flag and unverified beat", self.readme())

    def test_a_walk_that_runs_out_of_beats_on_its_own_still_ends(self):
        """Moving past the last read-only beat had no `done`, so the live page kept staging
        it as if the walk were still waiting."""
        text = self.skill()
        self.assertIn("When there is no next planned beat, the walk is over: post `done`", text)

    def test_a_question_from_the_page_resumes_the_walk_where_it_was(self):
        text = self.skill()
        self.assertIn("carry on from where the walk was, not from the beat the question was about", text)
        self.assertIn("park again if the walk was parked on an open `FLAG`", text)
        self.assertIn("if the walk had already ended, post `done` again and stay in Phase 4", text)
        self.assertNotIn("park again on the beat the question was asked about", text)
        self.assertNotIn("and wait after its answer", text)

    def test_next_is_walk_global_because_the_page_sends_no_beat_number(self):
        text = self.skill()
        self.assertIn("`next` is walk-global", text)
        self.assertIn("apply it from the walk's current position", text)

    def test_the_exemplars_show_both_verdicts(self):
        text = self.skill()
        self.assertIn("A clean beat, whose proof ran", text)
        self.assertIn("An unverified beat, whose proof was read", text)


class ReviewModeContract(unittest.TestCase):
    def skill(self):
        return skill_text()

    def test_review_findings_stay_accepted_until_the_review_is_posted(self):
        text = skill_text("SKILL.md")
        for anchor in (
            "In `review` mode",
            "If the working tree is dirty",
        ):
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, text)
        start = text.index("In `review` mode")
        end = text.index("If the working tree is dirty", start)
        rule = text[start:end]

        self.assertIn("keep the beat `accepted`", rule)
        self.assertIn("When the flag itself is a decision", rule)
        review_rule, decision_rule = rule.split("When the flag itself is a decision", 1)
        self.assertNotIn("post `decide`", review_rule)
        self.assertIn("post `decide`", decision_rule)

    def test_review_delivery_has_a_final_validation_and_publish_step(self):
        text = " ".join(skill_text("references/finish-review.md").split())
        self.assertIn("render-report.py $R --final", text)
        self.assertIn("one shared `lands[]` entry, atomically", text)
        self.assertIn("Pending or failed deliveries and unresolved review intents, including ones with no findings, are not final", text)
        self.assertIn("Follow the outstanding-review recovery guidance above", text)
        self.assertIn("re-publish the updated artifact", text)

    def test_final_render_exit_2_includes_zero_finding_review_intents_and_their_recovery(self):
        core = skill_text("SKILL.md")
        rule = core.split("Without `--final`", 1)[1].split("**Publishing is opt-in.**", 1)[0]
        prose = " ".join(rule.split())
        recovery = " ".join(skill_text("references/finish-review.md").split("**Review mode.**", 1)[0].split())

        self.assertIn("unresolved review intents, including ones with no findings", prose)
        self.assertIn("Exit 2 means the page rendered but a beat failed validation and carries an `UNPROVEN` chip, or `--final` found an unresolved review intent, even with no findings and no chip", prose)
        finish = core.split("## Phase 4: finish", 1)[1].split("## Session state", 1)[0]
        pointer = "If the audience is `review`, read `$S/references/finish-review.md` now, before rendering."
        self.assertEqual(finish.strip().split("\n\n", 1)[0], pointer)
        self.assertLess(finish.index(pointer), finish.index("render-report.py"))
        self.assertIn("For an outstanding review intent, follow the Review mode guidance in this file", recovery)
        active, terminal = recovery.split("For a `failed` or `retired` intent,", 1)
        self.assertIn("For a `prepared` or `submitting` intent, use `submit-review` to verify", active)
        self.assertIn("supervised `orphan-review` for a stored review that cannot land", active)
        self.assertIn("`retire-review` before a supervised replacement when no review id is stored", active)
        self.assertIn("prepare and preview a replacement, then get explicit approval before submitting it", terminal)
        for command in ("`submit-review`", "`orphan-review`", "`retire-review`"):
            self.assertNotIn(command, terminal)
        self.assertIn("For an open beat that failed validation", prose)

    def test_the_audience_shape_is_explicit(self):
        text = self.skill()
        self.assertIn("audience{mode: branch|review|report, why}", text)

    def test_pr_capture_freezes_audience_from_lifecycle(self):
        text = self.skill()

        self.assertIn("open PR | `review`", text)
        self.assertIn("non-open PR, merged or closed without merge | `report`", text)
        self.assertIn("target, `no-exec` policy, and derived audience in one transaction", text)
        self.assertIn("Never silently reclassify", text)
        self.assertIn("do not send it\nback through `patch-session`", text)


class ActionDeliveryContract(unittest.TestCase):
    def skill(self):
        return skill_text()

    def waiting_rule(self):
        text = skill_text("SKILL.md")
        start_anchor = "**Waiting on the reviewer.**"
        end_anchor = "**Resolving a flag.**"
        self.assertIn(start_anchor, text)
        self.assertIn(end_anchor, text)
        start = text.index(start_anchor)
        end = text.index(end_anchor, start)
        return text[start:end]

    def test_await_returns_the_oldest_unacknowledged_action(self):
        rule = self.waiting_rule()
        self.assertIn("oldest action that has not been acknowledged", rule)
        self.assertNotIn("start at `seq`", rule)
        self.assertNotIn("returned `handled_seq`", rule)

    def test_the_ack_comes_before_anything_the_reviewer_reads(self):
        """A walk that narrated its beat and then acked left the page holding every
        control in between, which on dailydeck#1499 read as a page that had died."""
        rule = " ".join(self.waiting_rule().split())
        self.assertIn("before you write anything the reviewer will read", rule)
        self.assertNotIn("acknowledge the exact reply before parking again", rule)

    def test_http_failures_are_visible(self):
        text = self.skill()
        self.assertNotIn("curl -s ", text)
        self.assertIn("curl -fsS", text)
        self.assertIn(
            "A transport failure or 5xx has an unknown outcome, so retry it unchanged.",
            text,
        )
        self.assertIn("A definite 4xx rejection may be corrected with a new ID.", text)

    def test_navigation_replay_uses_an_absolute_transactional_receipt(self):
        rule = self.waiting_rule()
        self.assertIn("absolute position and plan state", rule)
        self.assertIn("writes\n`current_beat` and `skipped_tiers` from it into the session", rule)
        self.assertIn("stored absolute\n`result` is the receipt", rule)
        self.assertNotIn("flat-file session", rule)
        self.assertIn("advances `current_beat` for a new beat", self.skill())

    def test_a_terminal_action_does_not_jump_the_queue(self):
        text = self.skill()
        self.assertIn("handle and acknowledge the older\nqueued action first", text)
        self.assertIn("do not apply this one again", text)


class QuestionContract(unittest.TestCase):
    def question_rule(self):
        text = " ".join(skill_text("SKILL.md").split())
        return text.split("**Answering a question.**", 1)[1].split(
            "A `next` on the last planned beat", 1
        )[0]

    def test_questions_have_a_distinct_evidence_qualified_application(self):
        rule = self.question_rule()

        self.assertIn('"kind":"question"', rule)
        self.assertIn('"answer":"Not proven at runtime."', rule)
        self.assertIn('"evidence":', rule)
        self.assertIn("`answer` and `evidence` must be nonempty text", rule)
        self.assertIn("inferred or insufficient", rule)
        self.assertIn("a question never authorizes executing its head", rule)
        self.assertIn("Do not include `session` or `beats`", rule)

    def test_answer_receipts_are_durable_before_the_terminal_handoff(self):
        rule = self.question_rule()

        self.assertLess(rule.index("Persist it"), rule.index("acknowledge its exact"))
        self.assertLess(rule.index("acknowledge its exact"), rule.index("in the terminal"))
        self.assertIn("already-applied `result` is the stored answer", rule)
        self.assertIn("Questions do not change the beat's state, `call`, delivery, cursor", rule)
        self.assertIn("do not advance the walk", rule)
        self.assertIn("visible pending status", rule)
        self.assertIn("Save note and Record decision are not question shortcuts", rule)
        self.assertNotIn("After answering a question, go straight to the next beat", skill_text())


class DelegatedActionContract(unittest.TestCase):
    def skill(self):
        return skill_text()

    def test_visible_actions_map_to_the_existing_protocol(self):
        text = self.skill()

        for label in (
            "Implement",
            "Include in review",
            "Include in report",
            "Record decision",
        ):
            with self.subTest(label=label):
                self.assertIn(label, text)
        self.assertIn("all post the canonical `accept` action", text)
        self.assertIn("Record decision posts the existing `decide` action", text)

    def test_decision_only_beats_declare_their_resolution_kind(self):
        text = self.skill()

        self.assertIn('resolution_kind: "decision"', text)
        self.assertIn('resolution_kind: "delivery"', text)
        self.assertIn("Imported legacy beats may omit the field", text)
        self.assertIn("moves the beat directly to `decided`", text)

    def test_branch_action_authorizes_later_implementation(self):
        text = self.skill()

        self.assertNotIn("a patch you have already written and run", text)
        self.assertNotIn("FIX    the patch", text)
        self.assertIn(
            "FIX    the implementation intent, review recommendation, or decision owed",
            text,
        )
        self.assertIn("after the click", text)
        self.assertIn("It does not\napprove an exact prepared patch.", text)
        self.assertIn("approved `FIX` intent remains unchanged", text)

    def test_final_render_rejects_unfinished_delegated_delivery(self):
        text = self.skill()

        self.assertIn("$S/scripts/render-report.py $R --final", text)
        self.assertIn("pending and failed delivery are valid live states", text)
        self.assertIn("They are never shippable final states", text)
        self.assertIn("An unknown outcome must never be marked failed", text)

    def test_readme_describes_delegated_actions(self):
        text = README.read_text(encoding="utf-8")

        self.assertNotIn("patch already\nwritten and run", text)
        self.assertIn("Implement applies and verifies a branch fix", text)
        self.assertIn("Include in review queues a finding", text)
        self.assertIn("Include in report records a finding", text)
        self.assertIn("Record decision stores an answer", text)
        self.assertIn("all record the existing `accept` action", text)


class ReportModeContract(unittest.TestCase):
    def skill(self):
        return skill_text()

    def test_report_accept_is_the_terminal_durable_outcome(self):
        text = self.skill()

        self.assertIn("In `report` mode, Include in report posts `accept`", text)
        self.assertIn("the accepted beat in SQLite is the durable report outcome", text)
        self.assertIn("every export rewrites `findings.md` from it", text)
        self.assertIn("Acknowledge the applied accept immediately", text)
        self.assertIn("Do not call `land`", text)
        self.assertIn("Acceptance freezes the agent-authored finding text", text)
        self.assertIn("acceptance validates and freezes the agent-authored finding", text)

    def test_report_mode_has_no_external_delivery(self):
        text = self.skill()

        self.assertIn("**Report mode.**", text)
        self.assertIn("does not create a GitHub effect", text)
        self.assertIn("`report.html` is a regenerable projection", text)
        self.assertIn("accepted report beats require no `landed` value", text)

    def test_github_422_is_treated_as_ambiguous(self):
        text = self.skill()

        self.assertNotIn("A 422 here means the audience call was wrong upstream", text)
        self.assertIn("Transport failures, 408, 429, 5xx, and 422 are uncertain", text)
        self.assertIn("Never blindly post twice or infer a new audience from a 422", text)
        self.assertIn("author cannot approve their own PR", text)


class PublishingIsOptIn(unittest.TestCase):
    """The page quotes the reviewed diff, and Phase 4 used to publish it to claude.ai as
    the normal end of a session with no question asked (DD-2361)."""

    def finish(self):
        text = " ".join(skill_text("SKILL.md").split())
        core_finish = text[text.index("## Phase 4: finish"):text.index("## Session state")]
        references = skill_references("finish-*.md")
        self.assertTrue(references)
        return core_finish + " " + " ".join(skill_text(*(
            path.relative_to(SKILL.parent).as_posix() for path in references
        )).split())

    def test_every_phase_four_render_writes_the_local_file(self):
        renders = [
            line for line in skill_text().splitlines()
            if "render-report.py $R" in line
        ]
        self.assertTrue(renders)
        for line in renders:
            with self.subTest(line=line):
                self.assertIn("--standalone", line)

    def test_the_local_file_is_the_default_outcome(self):
        rule = self.finish()
        self.assertNotIn("Publish with the `Artifact` tool. If that tool is unavailable", rule)
        self.assertIn("The finished page is the local file `$R/report.html`", rule)
        self.assertIn("nothing leaves this machine", rule)

    def test_publishing_waits_for_a_consent_that_names_the_diff_and_claude_ai(self):
        rule = self.finish()
        self.assertIn("**Publishing is opt-in.**", rule)
        self.assertIn("quoted lines of the reviewed diff", rule)
        self.assertIn("uploads all of that to claude.ai", rule)
        self.assertIn("Never publish unasked", rule)
        self.assertIn("**Keep it local**", rule)
        self.assertIn("**Publish to claude.ai**", rule)
        ask = rule.index("Publish only on an explicit yes")
        self.assertLess(rule.index("**Publishing is opt-in.**"), ask)

    def test_republishing_follows_an_earlier_yes_only(self):
        rule = self.finish()
        self.assertIn(
            "If the reviewer already agreed to publish an earlier render, re-publish the "
            "updated artifact",
            rule,
        )

    def test_readme_says_what_publishing_sends_where(self):
        text = " ".join(README.read_text(encoding="utf-8").split())
        finish = text[text.index("**Finish.**"):text.index("## Trust and execution")]
        self.assertIn("`report.html` in the session directory", finish)
        self.assertIn("only after you say yes", finish)
        self.assertIn("quoted lines of the reviewed diff", finish)
        self.assertIn("claude.ai", finish)
        page = text[text.index("## The page drives"):text.index("## Session state")]
        self.assertIn("stays on your machine unless you agree to publish it", page)


class FrozenSnapshotContract(unittest.TestCase):
    def skill(self):
        return skill_text()

    def test_ingest_freezes_a_local_exact_diff_before_contextual_reads(self):
        text = self.skill()
        core = skill_text("SKILL.md")
        snapshot = core.index('sessionctl.py snapshot-pr "$R"')
        context = core.index("After the snapshot is frozen")

        self.assertLess(snapshot, context)
        self.assertNotIn("gh pr diff", text)
        self.assertIn("local three-dot diff", core)
        self.assertIn("Run `check-pr` and `check-controller` again after those reads", core)

    def test_pr_snapshots_never_authorize_branch_side_effects(self):
        text = self.skill()

        self.assertIn("capture freezes the target, `no-exec` policy", text)
        self.assertIn("Do not check out or execute the head", text)
        self.assertIn("the page and store always refuse branch acceptance", text)
        self.assertIn("PR targets never enter this path", text)
        self.assertIn("supervised replacement session", text)
        self.assertIn("Never invoke it as review authorization", text)
        self.assertNotIn('check-worktree "$R" "$PWD"', text)

    def review_rule(self):
        return skill_text("references/finish-review.md").split("**Review mode.**", 1)[1]

    def test_review_prepares_previews_and_approves_the_intent_before_submission(self):
        rule = self.review_rule()
        prose = " ".join(rule.split())
        prepare = rule.index('prepare-review "$R"')
        preview = rule.index("Show the preview:")
        approval = rule.index("one explicit yes")
        submit = rule.index('submit-review "$R" --intent <intent-id>')
        final = rule.index('render-report.py $R --final --standalone')

        self.assertLess(prepare, preview)
        self.assertLess(preview, approval)
        self.assertLess(approval, submit)
        self.assertLess(submit, final)
        self.assertEqual(rule.count("one explicit yes"), 1)
        self.assertIn('--body-file "$R/review-body.md"', rule)
        self.assertIn('--comments-file "$R/review-comments.json"', rule)
        self.assertIn('"1": {"path":', rule)
        self.assertIn("exactly one entry per such beat, keyed by beat number", prose)
        self.assertIn("event, covered beats, and the complete `$R/review.fixed.json` payload", prose)
        self.assertIn("full frozen head to immutable payload bytes", prose)
        self.assertIn("validates anchors against the hash-verified diff", prose)
        self.assertIn("stable hidden delivery marker", prose)
        self.assertIn("`--replace` can replace only a never-posted prepared intent with no stored review id", prose)
        self.assertIn("With a stored id, use `submit-review` to verify or supervised `orphan-review`, not `--replace`", prose)
        self.assertNotIn("gh api", rule)
        self.assertNotIn("review-receipt", rule)
        self.assertNotIn('land "$R"', rule)
        self.assertNotIn("validate-anchors.py", rule)
        self.assertLess(len(rule.splitlines()), 100)

    def test_review_submission_owns_verification_landing_and_reconciliation(self):
        rule = self.review_rule()
        prose = " ".join(rule.split())

        self.assertIn("refuses changed preview bytes, runs `check-pr --require-open`", prose)
        self.assertIn("marks the intent submitting before the POST", prose)
        self.assertIn("stored actor, event/state, the validated body including folded findings, and inline comment content including multiline ranges", prose)
        self.assertIn("Only CRLF line endings and trailing body whitespace are normalized", prose)
        self.assertIn("storing the posting actor in the same transition", prose)
        self.assertIn("Reconciliation uses the stored actor even after an account switch", prose)
        self.assertIn("a different current actor cannot POST (exit 1, naming both logins)", prose)
        self.assertIn("lands exactly the intent's beats", prose)
        self.assertIn("Beats accepted later stay pending", prose)
        self.assertIn("Transport failures, 408, 429, 5xx, and 422 are uncertain", prose)
        self.assertIn("reconciles every page of reviews", prose)
        self.assertIn("more than one is a conflict", prose)
        self.assertIn("Exit 3 means a POST may exist", prose)
        self.assertIn("the intent stays submitting or keeps its stored review id, not failed", prose)
        self.assertIn("Exit 5 means could not check GitHub before posting: nothing posted, intent unchanged; rerun `submit-review`", prose)
        self.assertIn("a prepared intent's pre-POST read failure, with no stored review id", prose)
        self.assertIn("Repeat the same `submit-review` command to read again", prose)
        self.assertIn("does not POST again without `--retry`", prose)
        self.assertIn("repeat the read before seeking explicit approval for `--retry`", prose)
        self.assertIn("`DISMISSED` records a receipt without landing", prose)
        self.assertIn("Only an initial POST from a prepared intent can treat a definite other 4xx with no match as publication failure", prose)
        self.assertIn("A retry after an uncertain attempt stays submitting even on 4xx", prose)
        self.assertIn("An unknown outcome must never be marked failed", prose)
        self.assertIn("A stored review id is verified without reposting, even with `--retry`", prose)
        self.assertNotIn("--paginate", rule)

    def test_review_distinguishes_pre_post_drift_and_safe_retirement(self):
        rule = self.review_rule()
        prose = " ".join(rule.split())
        self.assertIn("Exit 4 means the target moved before POST: this invocation did not post, intent unchanged", prose)
        self.assertIn("For a submitting intent, an earlier POST may still surface", prose)
        self.assertIn('sessionctl.py retire-review "$R" --intent <intent-id>', prose)
        self.assertIn("before a supervised replacement", prose)
        self.assertIn("Retirement accepts a prepared intent, or a submitting one only after two zero-match", prose)
        self.assertIn("reconciliations under the submission lock, at least 30 seconds apart by default", prose)
        self.assertIn("`--reconcile-delay` configures seconds", prose)
        self.assertIn("it refuses any stored review id", prose)
        self.assertIn("A matching review lands with exit 0 and prints its receipt", prose)
        self.assertIn("Exit 2 now means posted but stale", prose)

    def test_review_documents_supervised_orphan_handling_without_repost(self):
        rule = self.review_rule()
        prose = " ".join(rule.split())
        self.assertIn('supervised `sessionctl.py orphan-review "$R" --intent <intent-id> --reason <text>`', prose)
        self.assertIn("records URL and reason, leaves beats pending, blocks repost", prose)
        self.assertLess(len(rule.splitlines()), 100)

    def test_review_keeps_verdict_stale_receipt_and_page_publication_rules(self):
        rule = self.review_rule()
        prose = " ".join(rule.split())

        self.assertIn("The verdict, approve versus request changes, is the reviewer's", prose)
        self.assertIn("`COMMENT`, `APPROVE`, or `REQUEST_CHANGES`", prose)
        self.assertIn("use `COMMENT` for self-review", prose)
        self.assertLess(rule.index('submit-review "$R"'), rule.index('check-pr "$R"'))
        self.assertIn("posted but stale", prose)
        self.assertIn("preserve the durable receipt", prose)
        self.assertIn("Do not retry the review", prose)
        self.assertIn("replay its durable landing, not to create another review", prose)
        self.assertIn("Publishing that page remains the separate opt-in under **Publishing is opt-in.** in `$S/SKILL.md`", prose)


class DropReasons(unittest.TestCase):
    """The article's noise measure sorts findings by how the author answered. A drop
    with no kind cannot be counted, so the skill, the store appendix and the README
    all name the same four."""

    FOUR = ("`wrong`", "`intentional`", "`not-worth-it`", "`pre-existing`")

    def test_resolving_a_flag_names_the_four_reasons_and_their_meanings(self):
        text = " ".join(skill_text("SKILL.md").split())
        rule = text.split("Drop asks for two things")[1].split("An answer in words")[0]
        for reason in self.FOUR:
            self.assertIn(reason, rule)
        self.assertIn("the PR did not introduce it", rule)
        self.assertIn("refuses a drop without both", rule)
        self.assertIn("ask which one when their words do not say", rule)

    def test_the_terminal_path_carries_the_reason_beside_the_note(self):
        text = " ".join(skill_text().split())
        self.assertIn('a drop carries `"reason"` beside its note', text)
        self.assertIn("is a drop whose words name its reason", text)

    def test_the_store_appendix_records_reason_as_a_new_field_only(self):
        text = " ".join(skill_text("SKILL.md").split())
        appendix = text.split("## Session state", 1)[1].split("`state` is one of `clean`", 1)[1]
        for reason in self.FOUR:
            self.assertIn(reason, appendix)
        self.assertIn("a beat dropped before the field existed has none", appendix)

    def test_the_readme_says_drop_asks_why(self):
        text = " ".join(README.read_text(encoding="utf-8").split())
        self.assertIn("Drop also asks which kind of miss it was", text)
        self.assertIn("wrong, intentional, not worth it or pre-existing", text)


class FlagAnchors(unittest.TestCase):
    """Rule 4 said no pre-existing flags and nothing checked; now the store does, and
    the FIX names the class when one fix applies in several places."""

    def test_rule_four_names_the_store_check_and_the_nearest_line(self):
        text = " ".join(skill_text("SKILL.md").split())
        rule = text.split("**4. One flag per beat")[1].split("**5.")[0]
        self.assertIn("inside the frozen diff's hunks, numbered as the head reads", rule)
        self.assertIn("context lines included", rule)
        self.assertIn("refused with the nearest touched line named", rule)

    def test_a_fix_that_applies_in_several_places_names_the_count(self):
        text = " ".join(skill_text().split())
        self.assertIn("`FIX` states the count and one example", text)

    def test_the_readme_says_a_flag_lands_on_a_touched_line(self):
        text = " ".join(README.read_text(encoding="utf-8").split())
        self.assertIn("refuses one on a line the PR never put in front of you", text)


class ReviewGuide(unittest.TestCase):
    """REVIEW.md was absent from the frozen governing set, so owner review guidance never
    reached the walk. This repository keeps one as the first consumer."""

    REVIEW = Path(__file__).resolve().parent.parent / "REVIEW.md"

    def skill(self):
        return " ".join(skill_text().split())

    def test_ingest_freezes_every_review_guide_from_the_base(self):
        self.assertIn("every `REVIEW.md` at any depth", self.skill())

    def test_the_claim_check_reads_what_failure_looks_like(self):
        text = " ".join(skill_text("SKILL.md").split())
        claim = text.split("**The claim check.**")[1].split("State the frozen audience")[0]
        self.assertIn("report a body that is silent on a failure the owners named", claim)
        self.assertIn("distinct from a mismatch", claim)

    def test_the_plan_batches_paths_the_guide_skips_and_says_so(self):
        text = " ".join(skill_text("SKILL.md").split())
        plan = text.split("## Phase 2: plan the walk")[1].split("## Phase 3")[0]
        self.assertIn("A file whose every hunk is what the frozen `REVIEW.md` covering it calls not worth walking goes to follow-through", plan)
        self.assertIn("the reviewer can pull it back", plan)

    def test_the_guide_is_not_a_license_to_skip_rule_four(self):
        text = self.skill()
        self.assertIn("the paths under its directory", text)
        self.assertIn("it is not a license to skip rule 4", text)

    def test_this_repository_keeps_a_guide_with_the_three_sections(self):
        text = self.REVIEW.read_text(encoding="utf-8")
        for heading in (
            "## What failure looks like here",
            "## Paths not worth walking",
            "## What counts as evidence",
        ):
            self.assertIn(heading, text)
        readme = " ".join(README.read_text(encoding="utf-8").split())
        self.assertIn("This repository keeps one at its root", readme)


class ClaimSections(unittest.TestCase):
    """The claim check compared the body against the diff only, so a PR that never said
    how it was tested produced no line anywhere. The requester answers Testing, Risk and
    How to revert; the review names what they left out."""

    TEMPLATE = Path(__file__).resolve().parent.parent / ".github" / "pull_request_template.md"

    def skill(self):
        return " ".join(skill_text().split())

    def claim(self):
        text = " ".join(skill_text("SKILL.md").split())
        return text.split("**The claim check.**")[1].split("State the frozen audience")[0]

    def test_the_claim_check_names_the_sections_the_body_lacks(self):
        claim = self.claim()
        self.assertIn("which of Testing, Risk and How to revert the body leaves unanswered", claim)
        self.assertIn("rather than folding them into a mismatch", claim)

    def test_a_missing_testing_answer_reaches_the_recommendation(self):
        text = " ".join(skill_text("SKILL.md").split())
        finish = text.split("## Phase 4: finish")[1].split("Render the page first")[0]
        claim = self.claim()
        self.assertIn("the body states no testing", claim)
        self.assertIn("the body states no testing", finish)
        self.assertIn("keeps that line through Phase 4", claim)
        self.assertIn("goes beside that line and never in its place", claim)
        self.assertNotIn("author states no testing", self.skill())

    def test_this_repository_asks_for_the_three_in_its_template(self):
        text = self.TEMPLATE.read_text(encoding="utf-8")
        for heading in ("## Testing", "## Risk", "## How to revert"):
            self.assertIn(heading, text)
        readme = " ".join(README.read_text(encoding="utf-8").split())
        self.assertIn("which of Testing, Risk and How to revert", readme)


class ReviewerNoise(unittest.TestCase):
    """The article's convergence fixes: measure noise before changing the reviewer, check
    every flag with a second read, and let a moved head reopen only what moved."""

    def skill(self):
        return " ".join(skill_text().split())

    def readme(self):
        return " ".join(README.read_text(encoding="utf-8").split())

    def test_the_ledger_is_the_guardrail_for_skill_changes(self):
        for text in (self.skill(), self.readme()):
            self.assertIn("sessionctl.py stats ~/.claude/reviews", text)
            self.assertIn("a change that raises the dropped share is a regression even when it finds more", text)

    def test_a_flag_is_read_twice_before_it_is_written(self):
        text = " ".join(skill_text("SKILL.md").split())
        walk = text.split("## Phase 3: walk")[1].split("**Persist the beat")[0]
        self.assertIn("A flag gets a second read before you write it.", walk)
        self.assertIn("Did this PR introduce it? Is it real? Would a senior engineer stop at it?", walk)
        self.assertIn("Do not pass your reasoning or your other beats", walk)
        self.assertIn("on whether it is real, demotes the flag before it is written", walk)
        self.assertIn("A no on whether a senior engineer would stop does not demote it", walk)
        self.assertIn("A stored flag cannot be demoted by a rewrite", walk)

    def test_a_replacement_links_its_predecessor_and_plans_only_what_moved(self):
        text = " ".join(skill_text("SKILL.md").split())
        replacement = " ".join(skill_text("references/replacement.md").split())
        ingest = text.split("## Phase 0")[1].split("## Phase 1")[0]
        self.assertIn('sessionctl.py link-predecessor "$R" "$P"', replacement)
        self.assertIn("never in it", replacement)
        self.assertIn("read `$S/references/replacement.md`", ingest)
        self.assertIn('sessionctl.py history "$R"', ingest)
        plan = text.split("## Phase 2: plan the walk")[1].split("## Phase 3")[0]
        self.assertIn("read `$S/references/replacement.md`", plan)
        self.assertIn("A replacement plans only what moved.", replacement)
        self.assertIn("it is not included again", replacement)

    def test_the_fourth_round_says_reconsider_the_approach(self):
        text = " ".join(skill_text("SKILL.md").split())
        orient = text.split("## Phase 1: orient")[1].split("## Phase 2")[0]
        self.assertIn("In round 4, the third replacement on one PR, the recommendation says to reconsider the approach", orient)
        self.assertIn("the recommendation says to reconsider the approach", self.readme())

    def test_the_review_history_promise_names_the_command_that_keeps_it(self):
        text = self.skill()
        self.assertNotIn("read the prior sessions for context", text)
        self.assertIn("`history` is how a later session reads it", text)


if __name__ == "__main__":
    unittest.main()
