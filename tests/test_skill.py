#!/usr/bin/env python3
"""Contract tests for the prompt-owned parts of the underwrite workflow."""
import unittest
from pathlib import Path


SKILL = Path(__file__).resolve().parent.parent / "skills" / "underwrite" / "SKILL.md"


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
        self.assertIn("audience{mode: branch|review, why}", text)


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


if __name__ == "__main__":
    unittest.main()
