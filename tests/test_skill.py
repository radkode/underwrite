#!/usr/bin/env python3
"""Contract tests for the prompt-owned parts of the underwrite workflow."""
import unittest
from pathlib import Path


SKILL = Path(__file__).resolve().parent.parent / "skills" / "underwrite" / "SKILL.md"
README = Path(__file__).resolve().parent.parent / "README.md"


class ReviewModeContract(unittest.TestCase):
    def skill(self):
        return SKILL.read_text(encoding="utf-8")

    def test_review_findings_stay_accepted_until_the_review_is_posted(self):
        text = self.skill()
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
        text = self.skill()
        self.assertIn("render-report.py $R --final", text)
        self.assertIn("shared entry is added to `lands[]` only once", text)
        self.assertIn("re-render the report", text)
        self.assertIn("re-publish the updated artifact", text)

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
        return SKILL.read_text(encoding="utf-8")

    def waiting_rule(self):
        text = self.skill()
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
        self.assertIn("stored absolute\n`result` is the receipt", rule)
        self.assertNotIn("flat-file session", rule)

    def test_a_terminal_action_does_not_jump_the_queue(self):
        text = self.skill()
        self.assertIn("handle and acknowledge the older\nqueued action first", text)
        self.assertIn("do not apply this one again", text)


class DelegatedActionContract(unittest.TestCase):
    def skill(self):
        return SKILL.read_text(encoding="utf-8")

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
        self.assertIn("Never mark an unknown outcome failed", text)

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
        return SKILL.read_text(encoding="utf-8")

    def test_report_accept_is_the_terminal_durable_outcome(self):
        text = self.skill()

        self.assertIn("In `report` mode, Include in report posts `accept`", text)
        self.assertIn("the accepted beat in SQLite is the durable report outcome", text)
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
        self.assertIn("A 422 is ambiguous", text)
        self.assertIn("stable marker and exact frozen head", text)
        self.assertIn("author cannot approve their own PR", text)


class FrozenSnapshotContract(unittest.TestCase):
    def skill(self):
        return SKILL.read_text(encoding="utf-8")

    def test_ingest_freezes_a_local_exact_diff_before_contextual_reads(self):
        text = self.skill()
        snapshot = text.index('sessionctl.py snapshot-pr "$R"')
        context = text.index("After the snapshot is frozen")

        self.assertLess(snapshot, context)
        self.assertNotIn("gh pr diff", text)
        self.assertIn("local three-dot diff", text)
        self.assertIn("Run `check-pr` and `check-controller` again after those reads", text)

    def test_pr_snapshots_never_authorize_branch_side_effects(self):
        text = self.skill()

        self.assertIn("capture freezes the target, `no-exec` policy", text)
        self.assertIn("Do not check out or execute the head", text)
        self.assertIn("the page and store always refuse branch acceptance", text)
        self.assertIn("PR targets never enter this path", text)
        self.assertIn("supervised replacement session", text)
        self.assertIn("Never invoke it as review authorization", text)
        self.assertNotIn('check-worktree "$R" "$PWD"', text)

    def test_review_validation_and_delivery_share_the_frozen_head(self):
        text = self.skill()
        start = text.index("**Review mode.**")
        rule = text[start:]

        self.assertIn('"commit_id": "<frozen target.head_sha>"', rule)
        self.assertIn('validate-anchors.py --session "$R"', rule)
        approval = rule.index("one explicit yes")
        preflight = rule.index('check-pr "$R" --require-open')
        post = rule.index("pulls/<n>/reviews --method POST")
        self.assertLess(approval, preflight)
        self.assertLess(preflight, post)
        self.assertIn('review-receipt "$R"', rule)
        self.assertIn('--actor "$ACTOR"', rule)
        self.assertIn("stable hidden delivery marker", rule)
        self.assertIn("requires exactly one non-pending review", rule)
        self.assertIn("`COMMENT`, `APPROVE`, or `REQUEST_CHANGES`", rule)
        self.assertIn("`DISMISSED`\nproves the post happened", rule)
        self.assertIn("Never match only `commit_id`", rule)
        self.assertIn("posted but stale", rule)


if __name__ == "__main__":
    unittest.main()
