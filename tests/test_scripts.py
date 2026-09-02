#!/usr/bin/env python3
"""Core-logic tests for both underwrite scripts. Python 3 stdlib, no deps.

    python3 -m unittest discover -s tests

Covers the parts that fail silently: what makes a beat shippable, what order
the report puts beats in, how a unified diff maps to anchorable lines, and
what happens to an anchor that does not land on one.
"""
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "underwrite" / "scripts"


def load(stem):
    """Import a script whose filename is not a legal module name."""
    spec = importlib.util.spec_from_file_location(
        stem.replace("-", "_"), SCRIPTS / (stem + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rr = load("render-report")
va = load("validate-anchors")


def beat(**kw):
    """A shippable clean beat, overridden by kw."""
    out = {
        "n": 1,
        "tier": "core",
        "state": "clean",
        "claim": "does what it says",
        "where": "a.ts:1",
        "slots": {"what": "adds a thing", "proof": "a.ts:1"},
    }
    out.update(kw)
    return out


def report_session(repo="r"):
    return {
        "repo": repo,
        "audience": {"mode": "report", "why": "the frozen PR is not open"},
        "target": {
            "kind": "github_pr",
            "state": "closed",
            "merged_at": None,
            "trusted_context_sha256": "a" * 64,
        },
        "execution_policy": {"trust": "untrusted", "mode": "no_exec"},
    }


def pr_target(state="closed", merged_at=None):
    return {
        "version": 1,
        "kind": "github_pr",
        "repo": "acme/widget",
        "number": 42,
        "state": state,
        "merged_at": merged_at,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "head_repo_id": 123,
        "head_repo": "acme/widget",
        "head_ref": "feature",
        "merge_base_sha": "c" * 40,
        "changed_files": 1,
    }


class BeatValidation(unittest.TestCase):
    def test_clean_beat_with_proof_is_shippable(self):
        self.assertEqual(rr.validate(beat()), [])

    def test_clean_beat_without_proof_is_not(self):
        problems = rr.validate(beat(slots={"what": "adds a thing"}))
        self.assertIn("clean with no proof", problems[0])

    def test_accepted_beat_also_needs_proof(self):
        problems = rr.validate(beat(state="accepted", slots={"what": "x"}))
        self.assertIn("accepted with no proof", problems[0])

    def test_a_final_accept_with_nothing_landed_is_not_shippable(self):
        """The reviewer said yes and the page had nothing to show for it. A real
        session shipped two beats in exactly this state."""
        problems = rr.validate(beat(state="accepted"), final=True)
        self.assertIn("accepted, nothing landed", problems[0])

    def test_an_accept_may_still_have_delegated_work_pending(self):
        self.assertEqual(rr.validate(beat(state="accepted")), [])

    def test_accepted_naming_what_it_landed_is_shippable(self):
        self.assertEqual(
            rr.validate(beat(
                state="accepted",
                landed="961eb58",
                delivery_kind="commit",
                branch="jacek/fix",
            )),
            [],
        )

    def test_landing_something_on_a_beat_nobody_resolved_is_not_shippable(self):
        """A decision given in words reached no server, so the state never moved. The
        rule above keys on `accepted` and looked straight past the one path it was
        built to catch: the fix committed, the beat still an open flag."""
        problems = rr.validate(beat(
            state="flag", landed="961eb58",
            slots={"what": "x", "proof": "a.ts:1", "risk": "r", "fix": "f"},
        ))
        self.assertIn("landed 961eb58 but state is 'flag'", problems[0])

    def test_review_mode_allows_an_unlanded_accept_before_the_post(self):
        """Phase 4 renders before it posts, so nothing has landed yet by design."""
        self.assertEqual(rr.validate(beat(state="accepted"), "review"), [])

    def test_a_final_review_requires_every_accept_to_name_the_post(self):
        problems = rr.validate(beat(state="accepted"), "review", final=True)
        self.assertIn("accepted, nothing landed", problems[0])

    def test_a_final_review_accept_naming_the_post_is_shippable(self):
        self.assertEqual(
            rr.validate(
                beat(
                    state="accepted",
                    landed="https://example.test/review/2",
                    delivery_kind="review",
                ),
                "review",
                final=True,
            ),
            [],
        )

    def test_a_final_report_accept_is_terminal_without_a_receipt(self):
        self.assertEqual(
            rr.validate(
                beat(state="accepted", delivery={"state": "none"}),
                "report",
                final=True,
            ),
            [],
        )

    def test_a_report_accept_rejects_delivery_receipt_fields(self):
        accepted = beat(
            state="accepted",
            landed="https://example.test/review/2",
            branch="jacek/fix",
            delivery_kind="review",
        )
        problems = rr.validate(accepted, "report", final=True)

        self.assertIn("report outcome cannot have delivery receipt fields", problems[0])
        self.assertIn("landed, branch, delivery_kind", problems[0])
        page = rr.render(
            report_session(),
            [accepted],
            "",
            {1: problems},
        )
        self.assertNotIn('<span class="lbl">Landed</span>', page)

    def test_a_report_accept_rejects_non_terminal_delivery_state(self):
        problems = rr.validate(
            beat(state="accepted", delivery={"state": "pending", "kind": "review"}),
            "report",
            final=True,
        )

        self.assertIn("report outcome cannot have pending delivery", problems[0])

    def test_a_landing_must_match_the_audience(self):
        branch = rr.validate(beat(
            state="accepted",
            landed="https://example.test/review/2",
            delivery_kind="review",
        ))
        review = rr.validate(
            beat(state="accepted", landed="abc1234", delivery_kind="commit"),
            "review",
            final=True,
        )

        self.assertIn("expected commit delivery", branch[0])
        self.assertIn("expected review delivery", review[0])

    def test_every_beat_needs_a_what(self):
        problems = rr.validate(beat(slots={"proof": "a.ts:1"}))
        self.assertIn("no what", problems[0])

    def test_flag_needs_risk_and_fix(self):
        problems = rr.validate(beat(state="flag", slots={"what": "x", "proof": "a.ts:1"}))
        self.assertEqual(len(problems), 2)
        self.assertIn("flag with no risk", problems[0])
        self.assertIn("flag with no fix", problems[1])

    def test_flag_with_risk_and_fix_is_shippable(self):
        self.assertEqual(
            rr.validate(
                beat(
                    state="flag",
                    slots={
                        "what": "x",
                        "proof": "`npm view pkg versions`",
                        "risk": "reddens a clean PR",
                        "fix": "pin it",
                    },
                )
            ),
            [],
        )

    def test_unknown_slot_is_rejected(self):
        problems = rr.validate(beat(slots={"what": "x", "proof": "a.ts:1", "notes": "y"}))
        self.assertIn("unknown slot 'notes'", problems[0])

    def test_slots_must_be_an_object(self):
        problems = rr.validate(beat(slots=["what", "proof"]))
        self.assertIn("slots must be an object", problems[0])

    def test_unknown_state_is_rejected(self):
        problems = rr.validate(beat(state="probably-fine"))
        self.assertIn("is not one of", problems[0])

    def test_resolution_kind_must_name_a_supported_control(self):
        problems = rr.validate(beat(resolution_kind="magic"))
        self.assertIn("resolution_kind must be delivery or decision", problems[0])

    def test_states_other_than_clean_and_accepted_need_no_proof(self):
        for state in ("flag", "unverified", "dropped"):
            slots = {"what": "x"}
            if state == "flag":
                slots.update(risk="r", fix="f")
            with self.subTest(state=state):
                self.assertEqual(rr.validate(beat(state=state, slots=slots)), [])


class DecidedBeats(unittest.TestCase):
    """The flag whose answer is a call rather than a patch. Accepting one used to demand
    a `landed` value it could never have, so Phase 4 refused to render it clean and the
    only fixes on offer were fabricating a SHA or overwriting the reviewer's state."""

    def test_a_decided_beat_owes_no_commit(self):
        self.assertEqual(rr.validate(beat(state="decided", call="stays as is")), [])

    def test_a_decided_beat_with_nothing_recorded_is_not_shippable(self):
        """The decision is the whole artifact, the way the SHA is for an accept."""
        problems = rr.validate(beat(state="decided"))
        self.assertIn("decided, nothing recorded", problems[0])

    def test_a_decided_beat_that_landed_something_is_a_contradiction(self):
        """If something shipped, it was accepted."""
        problems = rr.validate(beat(state="decided", call="c", landed="961eb58"))
        self.assertIn("landed 961eb58 but state is 'decided'", problems[0])

    def test_an_accepted_beat_still_owes_one(self):
        """The escape must not turn into a way around the rule it escapes."""
        self.assertIn(
            "accepted, nothing landed",
            rr.validate(beat(state="accepted"), final=True)[0],
        )

    def test_it_renders_among_the_beats_the_reviewer_said_yes_to(self):
        html = rr.render(
            {"repo": "r"}, [beat(n=1, state="decided", call="stays as is")], "", {})
        self.assertIn("Accepted", html)
        self.assertIn("DECIDED", html)
        self.assertNotIn("Unplaced", html)

    def test_the_decision_is_on_the_page_and_not_just_on_disk(self):
        html = rr.render(
            {"repo": "r"},
            [beat(n=1, state="decided", call="the cost lands on the caller")], "", {})
        self.assertIn("the cost lands on the caller", html)


class ProofEvidence(unittest.TestCase):
    """PROOF has to name something a reader can re-run or open."""

    def proof(self, value):
        return rr.validate(beat(slots={"what": "x", "proof": value}))

    def test_backticked_command_counts(self):
        self.assertEqual(self.proof("`npm view @scope/pkg versions` shows every major is 0"), [])

    def test_path_and_line_counts(self):
        self.assertEqual(self.proof("dist/index.js:1426 defaults it true"), [])

    def test_bare_prose_does_not_count(self):
        self.assertIn("names no command", self.proof("looked at it and it seemed fine")[0])

    def test_inferred_is_a_legal_value(self):
        """README and SKILL.md both declare `inferred` legal and honest."""
        self.assertEqual(self.proof("inferred"), [])

    def test_inferred_with_a_reason_is_legal(self):
        self.assertEqual(self.proof("inferred from the surrounding call sites"), [])

    def test_a_file_with_no_extension_counts(self):
        """Requiring a dot before the colon made these unproven, and they are ordinary
        review targets."""
        for path in ("Makefile:12", "Dockerfile:3", "CODEOWNERS:8", ".env:2"):
            with self.subTest(path=path):
                self.assertEqual(self.proof("%s pins it" % path), [])

    def test_a_clock_time_is_still_not_a_path(self):
        self.assertIn("names no command", self.proof("we met at 10:30 and agreed")[0])

    def test_a_proof_that_is_not_text_is_reported_not_raised(self):
        """Searching a number threw TypeError past main(), so the one step whose
        docstring promises never to fail did exactly that."""
        problems = self.proof(1426)
        self.assertIn("proof is int, not text", problems[0])


class ReportOrdering(unittest.TestCase):
    """The page is ordered by what is owed, not by beat number."""

    def render(self, beats):
        return rr.render({"repo": "acme/widget", "number": 42}, beats, "", {})

    def test_sections_run_flags_then_accepted_then_clean_then_dropped(self):
        html = self.render(
            [
                beat(n=1, state="dropped"),
                beat(n=2, state="clean"),
                beat(n=3, state="accepted"),
                beat(n=4, state="flag", slots={"what": "x", "risk": "r", "fix": "f"}),
            ]
        )
        order = [
            html.index("Needs your call"),
            html.index("Accepted"),
            html.index("Walked and clean"),
            html.index("Dropped"),
        ]
        self.assertEqual(order, sorted(order))

    def test_empty_sections_are_omitted(self):
        html = self.render([beat(n=1, state="clean")])
        self.assertIn("Walked and clean", html)
        self.assertNotIn("Needs your call", html)
        self.assertNotIn("Dropped", html)

    def test_flags_open_expanded_and_clean_beats_collapsed(self):
        # Matched loosely on purpose: attributes get added to these elements as
        # the page grows, and that must not read as a behavior change.
        flag = self.render([beat(n=1, state="flag", slots={"what": "x", "risk": "r", "fix": "f"})])
        clean = self.render([beat(n=1, state="clean")])
        self.assertRegex(flag, r'<details class="beat s-flag"[^>]*\sopen[\s>]')
        self.assertNotRegex(clean, r'<details class="beat s-clean"[^>]*\sopen[\s>]')

    def test_unverified_beats_count_as_clean_in_the_tiles(self):
        html = self.render([beat(n=1, state="clean"), beat(n=2, state="unverified")])
        self.assertRegex(html, r'<div class="count is-clean"[^>]*>\s*<span class="n">2</span>')

    def test_the_tiles_partition_every_walked_beat(self):
        """decided and dropped counted nowhere, so a walk with one recorded decision
        read 3 + 0 + 0 of 4."""
        html = self.render([
            beat(n=1, state="clean"),
            beat(n=2, state="flag", slots={"what": "x", "risk": "r", "fix": "f"}),
            beat(n=3, state="accepted", slots={"what": "x", "proof": "p:1", "fix": "f"}),
            beat(n=4, state="decided", slots={"what": "x", "risk": "r", "fix": "f"}),
            beat(n=5, state="dropped", slots={"what": "x", "risk": "r", "fix": "f"}),
            beat(n=6, state="unverified"),
        ])
        tiles = re.findall(r'<div class="count (is-[a-z]+)"[^>]*>\s*<span class="n">(\d+)</span>', html)
        self.assertEqual(
            tiles,
            [("is-clean", "2"), ("is-flag", "1"), ("is-acc", "2"), ("is-drop", "1"), ("is-mute", "6")],
        )
        self.assertEqual(sum(int(n) for _cls, n in tiles[:-1]), int(tiles[-1][1]))

    def test_a_beat_with_an_unplaceable_state_is_shown_not_dropped(self):
        """It matched no section and rendered nowhere, while still counting in the
        tiles: the page said four beats walked and showed three."""
        html = self.render([beat(n=1, state="clean"), beat(n=2, state="typoed")])
        self.assertIn("Unplaced", html)
        self.assertIn('data-n="2"', html)
        self.assertRegex(html, r'<div class="count is-mute"[^>]*>\s*<span class="n">2</span>')

    def test_no_unplaced_section_when_every_state_is_known(self):
        self.assertNotIn("Unplaced", self.render([beat(n=1, state="clean")]))

    def test_a_frozen_target_supplies_the_pr_identity(self):
        html = rr.render(
            {"target": {"repo": "acme/frozen", "number": 42, "head_sha": "a" * 40}},
            [beat(n=1, state="clean")],
            "",
            {},
        )
        self.assertIn("acme/frozen", html)
        self.assertIn("#42 underwrite", html)

    def test_a_failing_beat_carries_the_unproven_chip(self):
        html = rr.render({"repo": "r"}, [beat(n=1)], "", {1: ["beat 1: no what"]})
        self.assertIn("unproven", html)


class TheWalk(unittest.TestCase):
    """While a walk is listening the newest beat leads the page on its own, with the
    plan as a track above it and the ledger below; a finished walk and every final
    render are the ledger alone."""

    def session(self):
        return {
            "repo": "acme/widget", "number": 42,
            "plan": [
                {"n": 1, "tier": "enabling", "where": "a.ts:1"},
                {"n": 2, "tier": "core", "where": "b.ts:2"},
                {"n": 3, "tier": "core", "where": "c.ts:3"},
                {"n": 4, "tier": "follow-through", "where": "d.ts:4"},
            ],
        }

    def beats(self):
        return [
            beat(n=1, state="clean"),
            beat(n=2, state="decided", call="leave it", tier="core",
                 slots={"what": "x", "proof": "b.ts:2", "risk": "r", "fix": "f"}),
            beat(n=3, state="clean", tier="core", where="c.ts:3"),
        ]

    def live(self, beats, phase="parked"):
        return rr.render(self.session(), beats, "", {}, live=True, phase=phase)

    def test_the_newest_beat_is_staged_and_left_out_of_the_ledger(self):
        html = self.live(self.beats())
        stage = html.split('id="stage"')[1].split("</section>")[0]
        ledger = html.split("</section>", 1)[1]
        self.assertIn('data-n="3"', stage)
        self.assertNotIn('data-n="3"', ledger)
        self.assertIn('data-n="1"', ledger)
        self.assertIn("beat 3 of 4 · core", stage)
        # the stage row carries its own Next beat, after the note
        self.assertRegex(
            stage, r'<input class="note"[^>]*>\s*<button class="act" data-action="next">'
        )

    def test_the_ghost_of_the_next_planned_beat_is_hidden_until_between_beats(self):
        html = self.live(self.beats())
        self.assertIn('<div class="ghost" hidden>', html)
        self.assertIn("beat 4 of 4 · follow-through · d.ts:4", html)

    def test_before_the_first_beat_the_ghost_of_beat_one_shows(self):
        html = self.live([])
        self.assertIn('<div class="ghost">', html)
        self.assertIn("beat 1 of 4 · enabling · a.ts:1", html)
        self.assertIn("nothing walked yet", html)
        self.assertIn('aria-label="waiting for beat 1"', html)

    def test_the_track_marks_every_planned_beat_with_its_state(self):
        html = self.live(self.beats())
        track = html.split('<ol class="track"')[1].split("</ol>")[0]
        self.assertIn('aria-label="beat 3 of 4"', track)
        self.assertRegex(track, r'<li class="tk s-clean"[^>]*>1</li>')
        self.assertRegex(track, r'<li class="tk s-acc"[^>]*>2</li>')
        self.assertRegex(track, r'<li class="tk s-clean is-now" aria-current="step"[^>]*>3</li>')
        self.assertRegex(track, r'<li class="tk is-todo"[^>]*>4</li>')

    def test_a_done_walk_has_no_stage_and_every_beat_in_the_ledger(self):
        html = self.live(self.beats(), phase="done")
        self.assertNotIn('id="stage"', html)
        self.assertIn('data-n="3"', html)
        self.assertIn('aria-label="3 of 4 walked"', html)

    def test_a_final_render_has_neither_bar_nor_stage(self):
        html = rr.render(self.session(), self.beats(), "", {})
        self.assertNotIn('class="bar"', html)
        self.assertNotIn('id="stage"', html)
        self.assertNotIn('id="live"', html)

    def test_the_status_line_lives_in_the_bar_once(self):
        html = self.live(self.beats())
        self.assertEqual(html.count('id="live"'), 1)
        self.assertIn('id="live" class="live starting" role="status"', html)
        self.assertNotIn('class="acts walk" data-acts="walk">', html.split('id="live-body"')[0])

    def test_a_resolved_beat_folds_to_its_line_with_the_call_while_walking(self):
        html = self.live(self.beats())
        self.assertRegex(html, r'<details class="beat s-acc" data-n="2">')
        self.assertIn('<span class="b-call">“leave it”</span>', html)
        # and stays open once the walk is done, as the final report has it
        self.assertRegex(self.live(self.beats(), phase="done"),
                         r'<details class="beat s-acc" data-n="2" open>')


class DelegatedActionControls(unittest.TestCase):
    def flag(self, **kw):
        return beat(
            state="flag",
            slots={"what": "x", "proof": "a.ts:1", "risk": "r", "fix": "f"},
            **kw,
        )

    def render(self, session, b):
        return rr.render(session, [b], "", {}, live=True)

    def test_branch_flags_offer_implementation_through_the_compatible_action(self):
        html = self.render(
            {"repo": "r", "audience": {"mode": "branch"}}, self.flag()
        )

        self.assertIn(
            '<button class="act primary" data-action="accept">Implement</button>',
            html,
        )
        self.assertNotIn(">Accept</button>", html)

    def test_review_flags_offer_inclusion_through_the_compatible_action(self):
        html = self.render(
            {"repo": "r", "audience": {"mode": "review"}}, self.flag()
        )

        self.assertIn(
            '<button class="act primary" data-action="accept">Include in review</button>',
            html,
        )

    def test_report_flags_offer_inclusion_in_the_local_report(self):
        html = self.render(report_session(), self.flag())

        self.assertIn(
            '<button class="act primary" data-action="accept">Include in report</button>',
            html,
        )
        self.assertIn("Outcome: report only", html)
        self.assertNotIn("Include in review", html)

        included = rr.render(
            report_session(),
            [beat(state="accepted", delivery={"state": "none"})],
            "",
            {},
        )
        self.assertIn("Included in report", included)

        malformed = report_session()
        malformed["lands"] = [{
            "state": "ready",
            "what": "external work",
            "where": "elsewhere",
        }]
        without_lands = rr.render(malformed, [self.flag()], "", {})
        self.assertNotIn("What lands", without_lands)

    def test_unproven_report_flags_do_not_offer_inclusion(self):
        invalid = self.flag()
        invalid["slots"]["proof"] = "trust me"

        html = self.render(report_session(), invalid)

        self.assertIn("Complete finding evidence before inclusion", html)
        self.assertNotIn('data-action="accept">Include in report</button>', html)

    def test_a_pr_audience_that_conflicts_with_frozen_lifecycle_is_blocked(self):
        html = self.render(
            {
                "repo": "r",
                "audience": {"mode": "report"},
                "target": {
                    "kind": "github_pr",
                    "state": "open",
                    "merged_at": None,
                    "trusted_context_sha256": "a" * 64,
                },
                "execution_policy": {"trust": "untrusted", "mode": "no_exec"},
            },
            self.flag(),
        )

        self.assertIn("PR session requires a supervised replacement", html)
        self.assertNotIn('data-action="accept"', html)

    def test_historical_pr_branch_flags_require_replacement(self):
        html = self.render(
            {
                "repo": "r",
                "audience": {"mode": "branch"},
                "target": {
                    "kind": "github_pr",
                    "state": "open",
                    "merged_at": None,
                    "trusted_context_sha256": "a" * 64,
                },
                "execution_policy": {"trust": "untrusted", "mode": "no_exec"},
            },
            self.flag(),
        )

        self.assertIn("PR session requires a supervised replacement", html)
        self.assertNotIn('data-action="accept"', html)
        self.assertIn('data-action="drop"', html)

    def test_no_exec_review_flags_still_offer_inclusion(self):
        html = self.render(
            {
                "repo": "r",
                "audience": {"mode": "review"},
                "execution_policy": {"trust": "untrusted", "mode": "no_exec"},
            },
            self.flag(),
        )

        self.assertIn("Include in review", html)

    def test_a_pre_target_legacy_pr_hides_implementation(self):
        html = self.render(
            {
                "repo": "acme/widget",
                "number": 7,
                "legacy_pr": {"repo": "acme/widget", "number": 7},
                "audience": {"mode": "branch"},
            },
            self.flag(),
        )

        self.assertIn("PR session requires a supervised replacement", html)
        self.assertNotIn('data-action="accept"', html)
        self.assertIn('data-action="note">Save note</button>', html)
        self.assertIn("Execution: No-exec, legacy PR", html)

    def test_a_contextless_legacy_review_cannot_queue_delivery(self):
        html = self.render(
            {
                "repo": "acme/widget",
                "audience": {"mode": "review"},
                "target": {"kind": "github_pr"},
                "execution_policy": {"trust": "untrusted", "mode": "no_exec"},
            },
            self.flag(),
        )

        self.assertIn("PR session requires a supervised replacement", html)
        self.assertNotIn("Include in review", html)
        self.assertNotIn('data-action="accept"', html)

    def test_the_execution_policy_is_visible_in_the_masthead(self):
        html = self.render(
            {
                "repo": "r",
                "execution_policy": {"trust": "untrusted", "mode": "no_exec"},
            },
            self.flag(),
        )

        self.assertIn("Execution: No-exec", html)
        self.assertIn("Trust: untrusted PR head", html)

    def test_decision_only_flags_record_the_decision_directly(self):
        html = self.render(
            {"repo": "r", "audience": {"mode": "branch"}},
            self.flag(resolution_kind="decision"),
        )

        self.assertIn(
            '<button class="act primary" data-action="decide">Record decision</button>',
            html,
        )
        self.assertIn('placeholder="record the decision in your own words"', html)
        self.assertIn("fresh.action === 'decide' && !fresh.note", html)
        self.assertIn("enter the decision first", html)
        self.assertNotIn('data-action="accept"', html)

    def test_pending_delivery_is_described_by_audience(self):
        branch = rr.render(
            {"repo": "r", "audience": {"mode": "branch"}},
            [beat(
                state="accepted",
                delivery={"state": "pending", "kind": "commit"},
            )],
            "",
            {},
        )
        review = rr.render(
            {"repo": "r", "audience": {"mode": "review"}},
            [beat(
                state="accepted",
                delivery={"state": "pending", "kind": "review"},
            )],
            "",
            {},
        )

        self.assertIn("Implementation pending", branch)
        self.assertIn("Included, review pending", review)

    def test_failed_delivery_shows_the_failure_and_next_attempt(self):
        html = rr.render(
            {"repo": "r", "audience": {"mode": "branch"}},
            [beat(state="accepted", delivery={
                "state": "failed",
                "kind": "commit",
                "error": "tests <failed>",
                "owed": "repair `fixture.py:2`",
            })],
            "",
            {},
        )

        self.assertIn("Implementation failed", html)
        self.assertIn("tests &lt;failed&gt;", html)
        self.assertIn("Next attempt:", html)
        self.assertIn("repair <code>fixture.py:2</code>", html)

    def test_historical_pr_commit_delivery_requires_replacement(self):
        session = {
            "repo": "r",
            "audience": {"mode": "branch"},
            "target": {
                "kind": "github_pr",
                "state": "open",
                "merged_at": None,
                "trusted_context_sha256": "frozen",
            },
        }
        html = rr.render(
            session,
            [beat(state="accepted", delivery={
                "state": "pending",
                "kind": "commit",
            })],
            "",
            {},
        )
        failed = rr.render(
            session,
            [beat(state="accepted", delivery={
                "state": "failed",
                "kind": "commit",
                "error": "tests failed",
                "owed": "repair the fixture",
            })],
            "",
            {},
        )

        self.assertIn("Blocked, replacement required", html)
        self.assertIn("Do not publish or execute this delivery", html)
        self.assertNotIn("Implementation pending", html)
        self.assertIn("Recorded failure: tests failed", failed)
        self.assertIn("Previously recorded obligation: repair the fixture", failed)
        self.assertNotIn("Implementation failed", failed)
        self.assertNotIn("Next attempt:", failed)

    def test_contextless_pr_review_delivery_requires_replacement(self):
        sessions = (
            {
                "repo": "r",
                "audience": {"mode": "review"},
                "target": {"kind": "github_pr"},
                "execution_policy": {"trust": "untrusted", "mode": "no_exec"},
            },
            {
                "repo": "r",
                "number": 7,
                "audience": {"mode": "review"},
                "legacy_pr": {"repo": "r/project", "number": 7},
            },
        )
        for session in sessions:
            with self.subTest(session=session):
                html = rr.render(
                    session,
                    [beat(state="accepted", delivery={
                        "state": "pending",
                        "kind": "review",
                    })],
                    "",
                    {},
                )

                self.assertIn("Blocked, replacement required", html)
                self.assertIn("Do not publish or execute this delivery", html)
                self.assertNotIn("Included, review pending", html)

    def test_failed_review_delivery_names_publication(self):
        html = rr.render(
            {"repo": "r", "audience": {"mode": "review"}},
            [beat(state="accepted", delivery={
                "state": "failed",
                "kind": "review",
                "error": "request rejected",
                "owed": "retry the post",
            })],
            "",
            {},
        )

        self.assertIn("Review publication failed", html)

    def test_malformed_report_delivery_uses_report_specific_labels(self):
        pending = rr.render(
            report_session(),
            [beat(state="accepted", delivery={
                "state": "pending",
                "kind": "review",
            })],
            "",
            {},
        )
        failed = rr.render(
            report_session(),
            [beat(state="accepted", delivery={
                "state": "failed",
                "kind": "review",
            })],
            "",
            {},
        )

        self.assertIn("Report inclusion pending", pending)
        self.assertIn("Report inclusion failed", failed)
        self.assertNotIn("review pending", pending)
        self.assertNotIn("Review publication", failed)


class Escaping(unittest.TestCase):
    """Beat content is author-controlled but quotes code from the PR under review."""

    def test_markup_in_content_is_escaped(self):
        self.assertEqual(rr.md("<script>alert(1)</script>"), "&lt;script&gt;alert(1)&lt;/script&gt;")

    def test_backticks_become_code_after_escaping(self):
        self.assertEqual(rr.md("use `<T>` here"), "use <code>&lt;T&gt;</code> here")

    def test_ampersands_are_escaped(self):
        self.assertEqual(rr.md("a && b"), "a &amp;&amp; b")

    def test_md_leaves_quotes_alone_which_is_why_attributes_need_attr(self):
        self.assertEqual(rr.md('say "hi"'), 'say "hi"')
        self.assertEqual(rr.attr('say "hi"'), "say &quot;hi&quot;")

    def test_a_beat_number_cannot_break_out_of_its_attributes(self):
        """`n` is agent-written bookkeeping, but it went raw into three attributes and
        one text node while everything beside it was escaped."""
        html = rr.render(
            {"repo": "r"},
            [beat(n='2"><img src=x onerror=alert(9)>')],
            "", {}, live=True,
        )
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)

    def test_a_lands_state_cannot_add_an_event_handler(self):
        html = rr.render(
            {"repo": "r", "lands": [{"state": 'open" onmouseover="alert(1)', "what": "x"}]},
            [], "", {},
        )
        self.assertNotIn('onmouseover="alert(1)"', html)

    def test_the_pr_number_is_escaped_everywhere_it_appears(self):
        """It was escaped in the title and the h1 and raw in the eyebrow."""
        html = rr.render({"repo": "r", "number": "1</span><script>alert(1)</script>"}, [], "", {})
        self.assertNotIn("<script>", html)


class DiffParsing(unittest.TestCase):
    def test_maps_added_and_context_lines_to_sides(self):
        files = va.parse_diff(
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -10,3 +10,4 @@\n"
            " ctx\n"
            "-gone\n"
            "+new\n"
            "+also new\n"
        )
        self.assertEqual(files["x.py"]["RIGHT"], {10, 11, 12})
        self.assertEqual(files["x.py"]["LEFT"], {10, 11})

    def test_strips_the_b_prefix(self):
        files = va.parse_diff("+++ b/src/a.go\n@@ -0,0 +1 @@\n+x\n")
        self.assertIn("src/a.go", files)

    def test_a_deleted_file_maps_its_left_lines(self):
        """Deletions are walked last because they carry the most risk, so they have
        to stay anchorable. This replaces a test that asserted they were skipped."""
        files = va.parse_diff("--- a/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-x\n-y\n")
        self.assertEqual(files["gone.py"]["LEFT"], {1, 2})
        self.assertEqual(files["gone.py"]["RIGHT"], set())

    def test_a_new_file_has_no_left_lines(self):
        files = va.parse_diff("--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+x\n")
        self.assertEqual(files["new.py"]["RIGHT"], {1})
        self.assertEqual(files["new.py"]["LEFT"], set())

    def test_a_deletion_does_not_inherit_the_previous_path(self):
        files = va.parse_diff(
            "diff --git a/kept.py b/kept.py\n--- a/kept.py\n+++ b/kept.py\n@@ -1 +1 @@\n-a\n+b\n"
            "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
        )
        self.assertEqual(sorted(files), ["gone.py", "kept.py"])
        self.assertEqual(files["gone.py"]["LEFT"], {1})

    def test_a_quoted_unicode_path_decodes_to_utf8(self):
        files = va.parse_diff('+++ "b/caf\\303\\251.py"\n@@ -0,0 +1 @@\n+x\n')
        self.assertIn("café.py", files)

    def test_a_quoted_path_that_is_not_utf8_falls_back_instead_of_raising(self):
        files = va.parse_diff('+++ "b/a\\u00e9.py"\n@@ -0,0 +1 @@\n+x\n')
        self.assertEqual(len(files), 1)

    def test_hunk_header_without_counts_means_one_line(self):
        files = va.parse_diff("+++ b/x.py\n@@ -5 +5 @@\n-was\n+only\n")
        self.assertEqual(files["x.py"]["RIGHT"], {5})
        self.assertEqual(files["x.py"]["LEFT"], {5})

    def test_an_added_line_that_looks_like_a_header_is_not_one(self):
        """Counting by the header's totals is what keeps '++ x' from resyncing."""
        files = va.parse_diff("+++ b/x.py\n@@ -1,0 +1,2 @@\n+++ not a header\n+second\n")
        self.assertEqual(files["x.py"]["RIGHT"], {1, 2})

    def test_no_newline_marker_is_ignored(self):
        files = va.parse_diff("+++ b/x.py\n@@ -1 +1 @@\n+x\n\\ No newline at end of file\n")
        self.assertEqual(files["x.py"]["RIGHT"], {1})

    def test_a_separator_inside_a_line_does_not_end_it(self):
        """splitlines() breaks on nine separators beyond \\n, and one of them inside a
        line's content shattered the line and desynced the rest of the hunk. U+2028 is
        routine in minified JS and form feed in Emacs-formatted sources. The \\r case
        needs the reader to be byte-faithful too, which DiffReading covers."""
        for sep in ("\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"):
            with self.subTest(sep=sep):
                files = va.parse_diff(
                    "+++ b/x.py\n@@ -0,0 +1,3 @@\n+a%sb\n+second\n+third\n" % sep
                )
                self.assertEqual(files["x.py"]["RIGHT"], {1, 2, 3})

    def test_a_crlf_diff_parses_and_keeps_the_path_clean(self):
        files = va.parse_diff("--- a/x.py\r\n+++ b/x.py\r\n@@ -1 +1 @@\r\n-old\r\n+new\r\n")
        self.assertEqual(sorted(files), ["x.py"])
        self.assertEqual(files["x.py"]["RIGHT"], {1})

    def test_a_crlf_blank_context_line_still_counts(self):
        """A blank context line in a CRLF diff arrives as a bare \\r. Without the strip
        it matches no tag, hits the resync arm, and the rest of the hunk is dropped."""
        files = va.parse_diff(
            "--- a/x.py\r\n+++ b/x.py\r\n@@ -1,4 +1,4 @@\r\n a\r\n\r\n-c\r\n+d\r\n e\r\n"
        )
        self.assertEqual(files["x.py"]["RIGHT"], {1, 2, 3, 4})

    def test_several_files_stay_separate(self):
        files = va.parse_diff(
            "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+a\n"
            "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -0,0 +7 @@\n+b\n"
        )
        self.assertEqual(files["a.py"]["RIGHT"], {1})
        self.assertEqual(files["b.py"]["RIGHT"], {7})


class MultiLineRanges(unittest.TestCase):
    """GitHub rejects the whole review when a range crosses hunks or runs backwards,
    and a flat set of line numbers cannot see either."""

    DIFF = (
        "--- a/x.py\n+++ b/x.py\n"
        "@@ -10,2 +10,3 @@\n ctx\n+added\n ctx2\n"
        "@@ -30,2 +30,3 @@\n ctxb\n+addedb\n ctxb2\n"
    )

    def run_on(self, **comment):
        payload = {"body": "", "event": "COMMENT",
                   "comments": [dict(comment, path="x.py", body="n")]}
        report = va.validate(payload, va.parse_diff(self.DIFF))
        return payload["comments"][0], report

    def test_a_range_spanning_the_gap_between_hunks_is_dropped(self):
        """Both ends are in the diff, so the numbers alone said yes."""
        comment, report = self.run_on(
            line=31, side="RIGHT", start_line=11, start_side="RIGHT")
        self.assertNotIn("start_line", comment)
        self.assertIn("same hunk", report[0])

    # Earlier deletions push the old numbering ahead of the new one, which is the only
    # shape where comparing the two numbers gives a different answer than reading the
    # hunk. A start of 30 on LEFT genuinely precedes an end of 11 on RIGHT.
    SHIFTED = "--- a/y.py\n+++ b/y.py\n@@ -30,2 +10,3 @@\n-gone\n+new\n+more\n ctx\n"

    def test_a_mixed_side_range_inside_one_hunk_survives(self):
        """Old and new numbering are different coordinate spaces, so comparing them as
        numbers stripped ranges GitHub accepts."""
        payload = {"body": "", "event": "COMMENT", "comments": [
            {"path": "y.py", "line": 11, "side": "RIGHT",
             "start_line": 30, "start_side": "LEFT", "body": "n"}]}
        report = va.validate(payload, va.parse_diff(self.SHIFTED))
        self.assertEqual(payload["comments"][0]["start_line"], 30)
        self.assertEqual(report, [])

    def test_an_inverted_range_inside_one_hunk_is_still_dropped(self):
        comment, report = self.run_on(
            line=10, side="RIGHT", start_line=12, start_side="RIGHT")
        self.assertNotIn("start_line", comment)
        self.assertIn("come first", report[0])

    def test_an_ordinary_range_is_left_alone(self):
        comment, report = self.run_on(
            line=12, side="RIGHT", start_line=11, start_side="RIGHT")
        self.assertEqual(comment["start_line"], 11)
        self.assertEqual(report, [])

    def test_the_flat_sets_still_answer_the_single_line_question(self):
        files = va.parse_diff(self.DIFF)
        self.assertEqual(files["x.py"]["RIGHT"], {10, 11, 12, 30, 31, 32})
        self.assertEqual(len(files["x.py"]["hunks"]), 2)


class DiffReading(unittest.TestCase):
    """Whatever parse_diff does with a separator is moot if the read already ate it.
    Text mode turns a lone \\r into \\n, so the in-process tests above passed while the
    documented invocation, --diff on a file, still moved the anchor."""

    CR = (
        b"+++ b/web/app.js\n@@ -40,0 +41,3 @@\n"
        b'+const TIP = "press\renter";\n'
        b"+const KEY = process.env.SECRET;\n"
        b"+export default KEY;\n"
    )

    def run_cli(self, diff_bytes, line):
        """Run the script the way Phase 4 does. Returns (exit code, fixed payload)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "pr.diff").write_bytes(diff_bytes)
            (tmp / "payload.json").write_text(
                json.dumps({"body": "", "event": "COMMENT", "comments": [
                    {"path": "web/app.js", "line": line, "side": "RIGHT", "body": "n"}]}),
                encoding="utf-8",
            )
            done = subprocess.run(
                [sys.executable, str(SCRIPTS / "validate-anchors.py"),
                 "--diff", str(tmp / "pr.diff"),
                 "--payload", str(tmp / "payload.json"),
                 "--out", str(tmp / "fixed.json")],
                capture_output=True, text=True,
            )
            return done.returncode, json.loads(
                (tmp / "fixed.json").read_text(encoding="utf-8")
            )

    def test_a_bare_cr_read_from_a_file_does_not_move_the_anchor(self):
        code, payload = self.run_cli(self.CR, 43)
        self.assertEqual(code, 0)
        self.assertEqual(payload["comments"][0]["line"], 43)

    def test_a_clean_diff_still_validates_through_the_cli(self):
        code, payload = self.run_cli(self.CR.replace(b"\r", b""), 43)
        self.assertEqual(code, 0)
        self.assertEqual(payload["comments"][0]["line"], 43)

    def test_an_anchor_outside_the_diff_still_exits_2(self):
        """The exit code Phase 4 branches on, pinned through the real entry point."""
        code, payload = self.run_cli(self.CR, 900)
        self.assertEqual(code, 2)
        self.assertEqual(payload["comments"], [])


class RenderCli(unittest.TestCase):
    """Phase 4 branches on the exit code and then reads the file. Both halves of that
    were only ever exercised by hand."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "beats").mkdir()
        self.session({"repo": "acme/widget", "number": 42})

    def session(self, data):
        (self.root / "session.json").write_text(json.dumps(data), encoding="utf-8")

    def put(self, b):
        (self.root / "beats" / ("%02d.json" % b["n"])).write_text(
            json.dumps(b), encoding="utf-8")

    def frozen_store(self, state="closed", merged_at=None):
        (self.root / "session.json").unlink()
        source = self.root / "capture.diff"
        metadata = self.root / "capture.json"
        context = self.root / "trusted-context.json"
        source.write_text("diff\n", encoding="utf-8")
        metadata.write_text("{}\n", encoding="utf-8")
        context.write_text(
            json.dumps({
                "version": 1,
                "base_sha": "a" * 40,
                "files": [],
            }, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        store = rr.SessionStore(self.root)
        store.freeze_target(
            pr_target(state, merged_at), source, metadata, context
        )
        return store

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "render-report.py"), str(self.root), *args],
            capture_output=True, text=True,
        )

    def page(self, name="report.html"):
        return (self.root / name).read_text(encoding="utf-8")

    def test_a_clean_session_exits_0_and_writes_the_page(self):
        self.put(beat(n=1))
        done = self.run_cli()
        self.assertEqual(done.returncode, 0)
        self.assertIn("rendered 1 beats", done.stderr)
        self.assertIn("acme/widget", self.page())

    def test_an_unproven_beat_exits_2_and_still_writes_the_page(self):
        """Never fail at the last step of a session. The reviewer needs the page in
        order to see which beat to go and fix."""
        self.put(beat(n=1, slots={"what": "x"}))
        done = self.run_cli()
        self.assertEqual(done.returncode, 2)
        self.assertIn("clean with no proof", done.stderr)
        self.assertIn("unproven", self.page())

    def test_non_object_slots_render_as_unproven_instead_of_crashing(self):
        self.put(beat(n=1, slots=["what", "proof"]))

        done = self.run_cli()

        self.assertEqual(done.returncode, 2)
        self.assertIn("slots must be an object", done.stderr)
        self.assertIn("unproven", self.page())

    def test_a_review_accept_is_allowed_before_post_but_not_in_the_final_report(self):
        self.session({
            "repo": "acme/widget",
            "audience": {"mode": "review", "why": "another reviewer owns the PR"},
        })
        self.put(beat(n=1, state="accepted"))

        self.assertEqual(self.run_cli().returncode, 0)
        final = self.run_cli("--final")

        self.assertEqual(final.returncode, 2)
        self.assertIn("accepted, nothing landed", final.stderr)

    def test_a_branch_accept_is_allowed_during_delivery_but_not_in_the_final_report(self):
        self.session({
            "repo": "acme/widget",
            "audience": {"mode": "branch", "why": "the author owns the branch"},
        })
        self.put(beat(n=1, state="accepted"))

        self.assertEqual(self.run_cli().returncode, 0)
        final = self.run_cli("--final")

        self.assertEqual(final.returncode, 2)
        self.assertIn("accepted, nothing landed", final.stderr)

    def test_a_report_accept_is_terminal_in_the_final_report(self):
        store = self.frozen_store()
        finding = beat(
            n=1,
            state="flag",
            slots={
                "what": "x",
                "proof": "a.ts:1",
                "risk": "r",
                "fix": "f",
            },
        )
        store.put_beat(finding)
        action = store.produce("accept-1", 1, "accept", "include it")
        store.ack(action["seq"])
        store.export_json()
        (self.root / "session.sqlite3").unlink()

        final = self.run_cli("--final")

        self.assertEqual(final.returncode, 0)
        self.assertTrue((self.root / "session.sqlite3").exists())
        self.assertIn("Outcome: report only", self.page())
        self.assertNotIn("<h2>What lands</h2>", self.page())

    def test_a_report_with_zero_accepts_has_an_explicit_outcome(self):
        store = self.frozen_store()
        store.put_beat(beat(n=1, state="clean"))

        final = self.run_cli("--final")

        self.assertEqual(final.returncode, 0)
        self.assertIn("Outcome: report only", self.page())
        self.assertNotIn("<h2>What lands</h2>", self.page())

    def test_a_pr_audience_mismatch_fails_final_validation(self):
        store = self.frozen_store(state="open")
        store.put_beat(beat(n=1, state="clean"))
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            row = db.execute(
                "SELECT body_json FROM session WHERE singleton = 1"
            ).fetchone()
            session = json.loads(row[0])
            session["audience"] = {"mode": "report", "why": "historical"}
            db.execute(
                "UPDATE session SET body_json = ? WHERE singleton = 1",
                (json.dumps(session),),
            )

        final = self.run_cli("--final")

        self.assertEqual(final.returncode, 2)
        self.assertIn("does not match frozen PR lifecycle", final.stderr)

    def test_a_targetless_raw_report_is_rejected(self):
        self.session({
            "repo": "acme/widget",
            "audience": {"mode": "report", "why": "bypass delivery"},
        })
        self.put(beat(n=1, state="clean"))

        final = self.run_cli("--final")

        self.assertEqual(final.returncode, 1)
        self.assertIn("report audience requires a frozen PR target", final.stderr)

    def test_raw_json_cannot_forge_an_accepted_report(self):
        store = self.frozen_store()
        store.put_beat(beat(
            n=1,
            state="flag",
            slots={
                "what": "x",
                "proof": "a.ts:1",
                "risk": "r",
                "fix": "f",
            },
        ))
        store.export_json()
        path = self.root / "beats" / "01.json"
        finding = json.loads(path.read_text(encoding="utf-8"))
        finding["state"] = "accepted"
        path.write_text(json.dumps(finding), encoding="utf-8")
        (self.root / "session.sqlite3").unlink()

        final = self.run_cli("--final")

        self.assertEqual(final.returncode, 1)
        self.assertIn("accepted legacy beat 1 has no accept action", final.stderr)

    def test_a_final_review_with_its_url_exits_zero(self):
        review_url = "https://example.test/review/2"
        self.session({
            "repo": "acme/widget",
            "audience": {"mode": "review", "why": "another reviewer owns the PR"},
            "lands": [{
                "state": "landed",
                "what": "one accepted finding",
                "where": review_url,
            }],
        })
        self.put(beat(
            n=1,
            state="accepted",
            landed=review_url,
            delivery_kind="review",
        ))

        done = self.run_cli("--final")

        self.assertEqual(done.returncode, 0)
        self.assertIn("What lands", self.page())
        self.assertIn(review_url, self.page())

    def test_legacy_landed_beats_infer_the_delivery_kind(self):
        self.put(beat(
            n=1, state="accepted", landed="abc1234", branch="jacek/fix"
        ))
        self.assertEqual(self.run_cli("--final").returncode, 0)

        review_url = "https://example.test/review/2"
        self.session({
            "repo": "acme/widget",
            "audience": {"mode": "review", "why": "another reviewer owns the PR"},
        })
        self.put(beat(n=1, state="accepted", landed=review_url))
        self.assertEqual(self.run_cli("--final").returncode, 0)

    def test_a_malformed_audience_is_reported_without_crashing(self):
        self.session({"repo": "acme/widget", "audience": "review"})
        self.put(beat(n=1))

        done = self.run_cli()

        self.assertEqual(done.returncode, 2)
        self.assertIn("session audience must be an object", done.stderr)
        self.assertIn("acme/widget", self.page())

    def test_an_audience_object_requires_a_known_mode(self):
        self.session({"repo": "acme/widget", "audience": {"why": "other reviewers"}})
        self.put(beat(n=1))

        done = self.run_cli()

        self.assertEqual(done.returncode, 2)
        self.assertIn(
            "session audience mode must be branch, review, or report",
            done.stderr,
        )

    def test_a_legacy_pr_without_an_execution_policy_fails_closed(self):
        self.session({
            "target": {
                "kind": "github_pr",
                "repo": "acme/widget",
                "number": 42,
                "head_sha": "a" * 40,
            }
        })
        self.put(beat(n=1))

        done = self.run_cli()

        self.assertEqual(done.returncode, 2)
        self.assertIn("PR session has no execution policy", done.stderr)

    def test_a_cursor_that_disagrees_with_the_beats_exits_2(self):
        """The one problem no per-beat check can see: a beat that never got written
        leaves every beat that did valid."""
        self.session({"repo": "acme/widget", "cursor": 3})
        self.put(beat(n=1))
        done = self.run_cli()
        self.assertEqual(done.returncode, 2)
        self.assertIn("cursor is 3 but 1 beat files exist", done.stderr)

    def test_a_session_that_will_not_parse_exits_1(self):
        (self.root / "session.json").write_text("{ half written", encoding="utf-8")
        done = self.run_cli()
        self.assertEqual(done.returncode, 1)
        self.assertIn("render-report:", done.stderr)
        self.assertFalse((self.root / "report.html").exists())

    def test_the_database_wins_over_corrupt_legacy_exports(self):
        self.put(beat(n=1))
        store = rr.SessionStore(self.root)
        store.put_session(dict(store.snapshot()[0], title="authoritative"))
        (self.root / "session.json").write_text("{ stale", encoding="utf-8")
        (self.root / "beats" / "01.json").write_text("{ stale", encoding="utf-8")

        done = self.run_cli()

        self.assertEqual(done.returncode, 0)
        self.assertIn("authoritative", self.page())

    def test_the_database_projection_exposes_failed_delivery(self):
        self.session({
            "repo": "acme/widget",
            "audience": {"mode": "branch", "why": "the author owns the branch"},
        })
        self.put(beat(
            n=1,
            state="flag",
            slots={"what": "x", "proof": "a.ts:1", "risk": "r", "fix": "pin it"},
        ))
        store = rr.SessionStore(self.root)
        action = store.produce("accept-1", 1, "accept", "")
        store.fail(action["seq"], "tests failed", "repair the fixture")

        done = self.run_cli()

        self.assertEqual(done.returncode, 0)
        self.assertIn("Implementation failed", self.page())
        self.assertIn("tests failed", self.page())
        self.assertIn("Next attempt: repair the fixture", self.page())

    def test_a_usage_error_exits_1_rather_than_naming_a_beat_to_fix(self):
        """argparse spends 2 on this, and 2 already means "rendered, go fix a beat".
        A mistyped flag sent the walk looking for a page that was never written."""
        done = subprocess.run(
            [sys.executable, str(SCRIPTS / "render-report.py"), "--bogus", str(self.root)],
            capture_output=True, text=True,
        )
        self.assertEqual(done.returncode, 1)
        self.assertIn("render-report:", done.stderr)

    def test_standalone_adds_the_shell_and_plain_stays_a_fragment(self):
        """The viewport tag lives in the shell, and report.css has a 620px breakpoint
        that never fires without it."""
        self.put(beat(n=1))
        self.run_cli("--standalone")
        self.assertTrue(self.page().startswith("<!doctype html>"))
        self.assertIn("viewport", self.page())
        self.run_cli()
        self.assertNotIn("<!doctype", self.page())

    def test_live_adds_the_controls_and_plain_does_not(self):
        self.put(beat(n=1))
        self.run_cli("--live")
        self.assertIn('data-action="note"', self.page())
        self.run_cli()
        self.assertNotIn("data-action", self.page())

    def test_live_reuses_an_action_id_until_the_request_succeeds(self):
        self.put(beat(n=1))
        self.run_cli("--live")

        page = self.page()
        self.assertIn("underwrite.pending-action", page)
        self.assertIn("session_id: sessionId", page)
        self.assertIn("pending.session_id !== sessionId", page)
        self.assertIn("sessionId !== incomingSessionId", page)
        self.assertIn("location.reload()", page)
        self.assertIn("pending && !sameAction(pending, fresh)", page)
        self.assertIn("retry the saved", page)
        self.assertIn("if (pending && state.head_id === pending.id)", page)
        self.assertIn("sent.status >= 400 && sent.status < 500", page)
        self.assertIn("sendAction(pending)", page)
        self.assertIn("!connected || !usable || awaitingSeq !== null", page)
        self.assertIn("body: JSON.stringify(payload)", page)
        response_guard = page.index("if (!sent.ok)")
        self.assertGreater(page.index("remember(null)", response_guard), response_guard)

    def test_live_first_sync_and_content_swaps_preserve_drafts(self):
        self.put(beat(n=1))
        self.run_cli("--live")

        page = self.page()
        self.assertIn("const drafts = new Map", page)
        self.assertIn("note.value = drafts.get(row.dataset.acts)", page)
        self.assertIn("requestSwap(state.rev)", page)
        self.assertIn("if (!response.ok)", page)
        self.assertIn("rev = targetRev", page)
        self.assertIn("rev !== desiredRev", page)
        self.assertIn("restorePending()", page)
        self.assertIn(
            "enable(!sending && connected && usable && awaitingSeq === null)", page
        )
        self.assertIn("enable(connected && usable && awaitingSeq === null)", page)
        self.assertIn("receipt.seq > observedSeq", page)
        self.assertIn("observedSeq >= awaitingSeq", page)

    def test_out_puts_the_page_where_it_is_told(self):
        self.put(beat(n=1))
        self.run_cli("--out", str(self.root / "elsewhere.html"))
        self.assertIn("acme/widget", self.page("elsewhere.html"))
        self.assertFalse((self.root / "report.html").exists())


class AnchorCli(unittest.TestCase):
    """The exit codes Phase 4 reads and the frozen and standalone input modes."""

    DIFF = b"+++ b/x.py\n@@ -10,2 +10,3 @@\n ctx\n+a\n+b\n"

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        source = self.dir / "captured.diff"
        metadata = self.dir / "captured.json"
        source.write_bytes(self.DIFF)
        metadata.write_text("{}\n", encoding="utf-8")
        self.head = "b" * 40
        va.SessionStore(self.dir).freeze_target(
            {
                "version": 1,
                "kind": "github_pr",
                "repo": "acme/widget",
                "number": 42,
                "state": "open",
                "merged_at": None,
                "base_sha": "a" * 40,
                "head_sha": self.head,
                "head_repo_id": 123,
                "head_repo": "acme/widget",
                "head_ref": "feature",
                "merge_base_sha": "c" * 40,
                "changed_files": 1,
            },
            source,
            metadata,
        )
        self.payload(11)

    def payload(self, line, commit_id=None):
        payload = {"body": "head", "event": "COMMENT", "comments": [
            {"path": "x.py", "line": line, "side": "RIGHT", "body": "n"}]}
        if commit_id is not None:
            payload["commit_id"] = commit_id
        (self.dir / "review.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

    def run_cli(self, *args, **kw):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "validate-anchors.py"), *args],
            capture_output=True, text=True, cwd=str(self.dir), **kw,
        )

    def test_a_valid_anchor_exits_0_and_says_so(self):
        done = self.run_cli("--diff", "pr.diff", "--payload", "review.json")
        self.assertEqual(done.returncode, 0)
        self.assertIn("all anchors valid", done.stderr)
        self.assertEqual(json.loads(done.stdout)["comments"][0]["line"], 11)

    def test_with_no_out_the_corrected_payload_goes_to_stdout(self):
        self.payload(15)
        done = self.run_cli("--diff", "pr.diff", "--payload", "review.json")
        self.assertEqual(done.returncode, 2)
        self.assertEqual(json.loads(done.stdout)["comments"][0]["line"], 12)
        self.assertIn("snap", done.stderr)

    def test_a_diff_on_stdin_is_read_the_same_way(self):
        done = self.run_cli(
            "--diff", "-", "--payload", "review.json", input=self.DIFF.decode())
        self.assertEqual(done.returncode, 0)

    def test_session_mode_uses_the_frozen_diff_and_injects_the_head(self):
        done = self.run_cli("--session", str(self.dir), "--payload", "review.json")
        self.assertEqual(done.returncode, 0)
        self.assertIn("all anchors valid", done.stderr)
        payload = json.loads(done.stdout)
        self.assertEqual(payload["commit_id"], self.head)
        self.assertIn("<!-- underwrite-review:", payload["body"])

    def test_session_mode_rejects_a_conflicting_delivery_marker(self):
        payload = json.loads((self.dir / "review.json").read_text(encoding="utf-8"))
        payload["body"] += "\n\n<!-- underwrite-review:" + "0" * 32 + " -->"
        (self.dir / "review.json").write_text(json.dumps(payload), encoding="utf-8")

        done = self.run_cli("--session", str(self.dir), "--payload", "review.json")

        self.assertEqual(done.returncode, 1)
        self.assertIn("conflicting delivery marker", done.stderr)

    def test_session_mode_rejects_a_conflicting_commit_id(self):
        self.payload(11, commit_id=self.head[:12])
        done = self.run_cli("--session", str(self.dir), "--payload", "review.json")
        self.assertEqual(done.returncode, 1)
        self.assertIn("does not match the frozen head", done.stderr)

    def test_session_mode_refuses_a_pending_review(self):
        payload = json.loads((self.dir / "review.json").read_text(encoding="utf-8"))
        payload.pop("event")
        (self.dir / "review.json").write_text(json.dumps(payload), encoding="utf-8")

        done = self.run_cli("--session", str(self.dir), "--payload", "review.json")

        self.assertEqual(done.returncode, 1)
        self.assertIn("not leave it pending", done.stderr)

    def test_session_mode_rejects_a_tampered_diff(self):
        (self.dir / "pr.diff").write_bytes(b"x" * len(self.DIFF))
        done = self.run_cli("--session", str(self.dir), "--payload", "review.json")
        self.assertEqual(done.returncode, 1)
        self.assertIn("pr.diff does not match", done.stderr)

    def test_a_payload_that_will_not_parse_exits_1(self):
        (self.dir / "review.json").write_text("{ half written", encoding="utf-8")
        done = self.run_cli("--diff", "pr.diff", "--payload", "review.json")
        self.assertEqual(done.returncode, 1)
        self.assertIn("validate-anchors:", done.stderr)

    def test_a_diff_that_is_not_there_exits_1(self):
        done = self.run_cli("--diff", "nope.diff", "--payload", "review.json")
        self.assertEqual(done.returncode, 1)

    def test_forgetting_the_diff_exits_1_rather_than_looking_like_a_fixed_anchor(self):
        """2 means "anchors moved, carry on", and Phase 4 then posts --out. Left at
        argparse's own 2, that is whatever the run before it happened to write."""
        done = self.run_cli("--payload", "review.json")
        self.assertEqual(done.returncode, 1)
        self.assertIn("validate-anchors:", done.stderr)


class Snapping(unittest.TestCase):
    def test_nearest_valid_line_wins(self):
        self.assertEqual(va.snap(10, {3, 8, 20}), 8)

    def test_ties_prefer_the_earlier_line(self):
        self.assertEqual(va.snap(10, {8, 12}), 8)

    def test_beyond_the_window_returns_nothing(self):
        self.assertIsNone(va.snap(100, {1, 2}))

    def test_exactly_at_the_window_edge_still_snaps(self):
        self.assertEqual(va.snap(100, {100 - va.SNAP_WINDOW}), 100 - va.SNAP_WINDOW)

    def test_no_valid_lines_returns_nothing(self):
        self.assertIsNone(va.snap(10, set()))


class AnchorValidation(unittest.TestCase):
    DIFF = "+++ b/x.py\n@@ -10,2 +10,3 @@\n ctx\n+a\n+b\n"

    def run_on(self, comments, body="head"):
        payload = {"body": body, "event": "COMMENT", "comments": comments}
        report = va.validate(payload, va.parse_diff(self.DIFF))
        return payload, report

    def test_a_valid_anchor_is_left_alone(self):
        payload, report = self.run_on([{"path": "x.py", "line": 11, "side": "RIGHT", "body": "ok"}])
        self.assertEqual(report, [])
        self.assertEqual(payload["comments"][0]["line"], 11)

    def test_a_near_miss_is_snapped(self):
        payload, report = self.run_on([{"path": "x.py", "line": 15, "side": "RIGHT", "body": "ok"}])
        self.assertEqual(payload["comments"][0]["line"], 12)
        self.assertIn("snap", report[0])

    def test_a_file_outside_the_diff_is_folded_into_the_body(self):
        payload, report = self.run_on([{"path": "other.py", "line": 3, "side": "RIGHT", "body": "note"}])
        self.assertEqual(payload["comments"], [])
        self.assertIn("note", payload["body"])
        self.assertIn("other.py", payload["body"])
        self.assertIn("fold", report[0])

    def test_folding_keeps_the_original_body(self):
        payload, _ = self.run_on(
            [{"path": "other.py", "line": 3, "side": "RIGHT", "body": "note"}], body="original"
        )
        self.assertTrue(payload["body"].startswith("original"))

    def test_an_unsnappable_line_in_a_known_file_is_folded(self):
        payload, report = self.run_on([{"path": "x.py", "line": 900, "side": "RIGHT", "body": "far"}])
        self.assertEqual(payload["comments"], [])
        self.assertIn("no hunk within", report[0])

    def test_a_stale_start_line_is_dropped_when_the_anchor_moves(self):
        payload, _ = self.run_on(
            [{"path": "x.py", "line": 15, "side": "RIGHT", "start_line": 14, "start_side": "RIGHT", "body": "r"}]
        )
        comment = payload["comments"][0]
        self.assertNotIn("start_line", comment)
        self.assertNotIn("start_side", comment)

    def test_side_is_respected(self):
        payload, _ = self.run_on([{"path": "x.py", "line": 10, "side": "LEFT", "body": "ok"}])
        self.assertEqual(payload["comments"][0]["line"], 10)

    def test_an_invalid_start_line_is_dropped_even_when_the_end_is_valid(self):
        """The end being valid used to return early, so the start was never looked at
        and GitHub rejected the whole review."""
        payload, report = self.run_on(
            [{"path": "x.py", "line": 11, "side": "RIGHT", "start_line": 999,
              "start_side": "RIGHT", "body": "multiline"}]
        )
        self.assertNotIn("start_line", payload["comments"][0])
        self.assertIn("dropped start_line 999", report[0])

    def test_an_inverted_range_is_dropped(self):
        payload, report = self.run_on(
            [{"path": "x.py", "line": 11, "side": "RIGHT", "start_line": 12,
              "start_side": "RIGHT", "body": "inverted"}]
        )
        self.assertNotIn("start_line", payload["comments"][0])
        self.assertIn("dropped start_line 12", report[0])

    def test_a_valid_range_is_left_alone(self):
        payload, report = self.run_on(
            [{"path": "x.py", "line": 12, "side": "RIGHT", "start_line": 11,
              "start_side": "RIGHT", "body": "range"}]
        )
        self.assertEqual(report, [])
        self.assertEqual(payload["comments"][0]["start_line"], 11)

    def test_start_line_is_checked_against_its_own_side(self):
        """GitHub allows start_side to differ from side. Checking the start against
        side's lines passes a start that is not on the side it claims."""
        payload, _ = self.run_on(
            [{"path": "x.py", "line": 12, "side": "RIGHT", "start_line": 11,
              "start_side": "LEFT", "body": "mixed"}]
        )
        self.assertNotIn("start_line", payload["comments"][0])

    def test_a_side_with_no_lines_says_so_rather_than_file_not_in_diff(self):
        files = va.parse_diff("--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+x\n")
        payload = {"body": "b", "event": "COMMENT",
                   "comments": [{"path": "new.py", "line": 1, "side": "LEFT", "body": "n"}]}
        report = va.validate(payload, files)
        self.assertIn("no LEFT lines", report[0])

    def test_a_separator_in_the_content_no_longer_moves_a_comment(self):
        """The desync was silent, so the anchor did not fail: it fell through to snap()
        and the note about `export default KEY` got posted on the line two above it."""
        diff = (
            "+++ b/web/app.js\n@@ -40,0 +41,3 @@\n"
            '+const TIP = "press\u2028enter";\n'
            "+const KEY = process.env.SECRET;\n"
            "+export default KEY;\n"
        )
        payload = {"body": "", "event": "COMMENT", "comments": [
            {"path": "web/app.js", "line": 43, "side": "RIGHT", "body": "exports a secret"}]}
        report = va.validate(payload, va.parse_diff(diff))
        self.assertEqual(report, [])
        self.assertEqual(payload["comments"][0]["line"], 43)

    def test_a_non_integer_line_is_folded_rather_than_crashing(self):
        payload, report = self.run_on([{"path": "x.py", "line": None, "side": "RIGHT", "body": "n"}])
        self.assertEqual(payload["comments"], [])
        self.assertIn("fold", report[0])


if __name__ == "__main__":
    unittest.main()
