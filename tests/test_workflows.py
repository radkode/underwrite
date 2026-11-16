#!/usr/bin/env python3
"""Composed tests for the prompt-owned branch and review workflows."""

import json
import stat
import sys
import unittest

if __package__:
    from .workflow_support import ROOT, WorkflowCase, flag
else:
    from workflow_support import ROOT, WorkflowCase, flag


class BranchWorkflow(WorkflowCase):
    def setUp(self):
        super().setUp()
        self.session = self.root / "branch-session"
        self.repo = self.root / "branch-repo"
        self.git("init", "--template=", self.repo)
        self.git("config", "user.name", "Underwrite Test", cwd=self.repo)
        self.git("config", "user.email", "underwrite@example.test", cwd=self.repo)
        self.git("config", "commit.gpgsign", "false", cwd=self.repo)
        (self.repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.git("add", "app.py", cwd=self.repo)
        self.git("commit", "-m", "base", cwd=self.repo)
        self.git("branch", "-M", "feature", cwd=self.repo)

        self.script_json("sessionctl.py", "init", self.session)
        self.script_json(
            "sessionctl.py",
            "put-session",
            self.session,
            value={
                "repo": "acme/widget",
                "cursor": 1,
                "lands": [],
                "audience": {
                    "mode": "branch",
                    "why": "the reviewer owns this local branch",
                },
            },
        )
        self.original = flag(1, "the value is not pinned", 1, "pin the value")
        self.script_json(
            "sessionctl.py", "put-beat", self.session, value=self.original
        )

    def test_offline_implementation_recovers_from_failure_and_stale_input(self):
        base_commit = self.git("rev-parse", "HEAD", cwd=self.repo)
        stale = self.script_json("sessionctl.py", "get-beat", self.session, 1)
        first, url = self.start_server(self.session)
        _status, state = self.request(url, "/state")
        action_body = {
            "id": "branch-accept",
            "session_id": state["session_id"],
            "n": 1,
            "action": "accept",
            "note": "implement it",
        }
        status, action = self.request(url, "/act", body=action_body)
        self.assertEqual(status, 200)
        self.stop_server(first)

        second, url = self.start_server(self.session)
        self.assertEqual(self.request(url, "/await")[1], action)
        restarted = self.script_json("sessionctl.py", "reconcile", self.session)
        self.assertEqual(restarted["head"]["seq"], action["seq"])
        self.assertEqual(restarted["pending_deliveries"][0]["beat_n"], 1)
        retry_status, retry = self.request(url, "/act", body=action_body)
        self.assertEqual(retry_status, 200)
        self.assertEqual(retry, action)
        self.stop_server(second)

        (self.repo / "app.py").write_text("VALUE = 0\n", encoding="utf-8")
        verification = self.run_command(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; raise SystemExit(0 if "
                "Path('app.py').read_text() == 'VALUE = 2\\n' else 7)",
            ],
            cwd=self.repo,
            expected=7,
        )
        failed = self.script_json(
            "sessionctl.py",
            "fail",
            self.session,
            action["seq"],
            f"verification exited {verification.returncode}",
            "pin the value and rerun verification",
        )
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.repo), base_commit)
        self.assertEqual(self.git("status", "--short", cwd=self.repo), "M app.py")

        stale["slots"]["fix"] = "replace the approved intent"
        rewritten = self.script_json(
            "sessionctl.py", "put-beat", self.session, value=stale
        )
        self.assertEqual(rewritten["state"], "accepted")
        self.assertEqual(rewritten["call"], "implement it")
        self.assertEqual(rewritten["slots"]["fix"], "pin the value")
        recovery = self.script_json("sessionctl.py", "reconcile", self.session)
        self.assertEqual(recovery["head"]["seq"], action["seq"])
        self.assertEqual(recovery["failed_deliveries"][0]["beat_n"], 1)
        self.assertEqual(
            recovery["failed_deliveries"][0]["error"],
            "verification exited 7",
        )

        unfinished = self.script(
            "render-report.py", self.session, "--final", expected=2
        )
        self.assertIn("accepted, nothing landed", unfinished.stderr)
        self.assertIn("accepted, failed delivery", unfinished.stderr)
        self.assertIn(
            "Implementation failed",
            (self.session / "report.html").read_text(encoding="utf-8"),
        )

        (self.repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        successful_verification = self.run_command(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; raise SystemExit(0 if "
                "Path('app.py').read_text() == 'VALUE = 2\\n' else 7)",
            ],
            cwd=self.repo,
        )
        self.assertEqual(successful_verification.returncode, 0)
        self.git("add", "app.py", cwd=self.repo)
        self.git("commit", "-m", "fix: pin the value", cwd=self.repo)
        commit = self.git("rev-parse", "HEAD", cwd=self.repo)
        landed = self.script_json(
            "sessionctl.py",
            "land",
            self.session,
            action["seq"],
            1,
            commit,
            "--kind",
            "commit",
            "--branch",
            "feature",
        )
        self.assertEqual(landed["artifact"], commit)
        self.assertEqual(
            self.script_json("sessionctl.py", "ack", self.session, action["seq"]),
            {"handled_seq": action["seq"]},
        )
        self.assertFalse(
            self.script_json("sessionctl.py", "reconcile", self.session)["recovery"]
        )

        self.script("render-report.py", self.session, "--final", "--standalone")
        page = (self.session / "report.html").read_text(encoding="utf-8")
        self.assertIn(commit, page)
        self.assertIn("Landed", page)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.repo), commit)


class ReviewWorkflow(WorkflowCase):
    def setUp(self):
        super().setUp()
        self.remote = self.root / "upstream.git"
        self.controller = self.root / "controller"
        self.session = self.root / "review-session"
        self.git("init", "--bare", "--template=", self.remote)
        self.git("init", "--template=", self.controller)
        self.git("config", "user.name", "Underwrite Test", cwd=self.controller)
        self.git(
            "config", "user.email", "underwrite@example.test", cwd=self.controller
        )
        self.git("config", "commit.gpgsign", "false", cwd=self.controller)
        (self.controller / "common.txt").write_text("common\n", encoding="utf-8")
        self.git("add", "common.txt", cwd=self.controller)
        self.git("commit", "-m", "common", cwd=self.controller)
        self.git("branch", "-M", "main", cwd=self.controller)
        self.git("remote", "add", "origin", self.remote, cwd=self.controller)
        self.git("push", "origin", "main", cwd=self.controller)

        self.git("checkout", "-b", "feature", cwd=self.controller)
        (self.controller / "app.py").write_text(
            "VALUE = 1\nTIMEOUT = 5\n", encoding="utf-8"
        )
        self.git("add", "app.py", cwd=self.controller)
        self.git("commit", "-m", "feature", cwd=self.controller)
        self.head = self.git("rev-parse", "HEAD", cwd=self.controller)
        self.git(
            "push", "origin", "HEAD:refs/pull/7/head", cwd=self.controller
        )

        self.git("checkout", "main", cwd=self.controller)
        (self.controller / "base.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "base.txt", cwd=self.controller)
        self.git("commit", "-m", "base", cwd=self.controller)
        self.base = self.git("rev-parse", "HEAD", cwd=self.controller)
        self.git("push", "origin", "main", cwd=self.controller)

        self.metadata = {
            "number": 7,
            "state": "open",
            "merged_at": None,
            "changed_files": 1,
            "base": {
                "ref": "main",
                "sha": self.base,
                "repo": {
                    "full_name": "acme/widget",
                    "clone_url": str(self.remote),
                },
            },
            "head": {
                "ref": "feature",
                "sha": self.head,
                "repo": {"id": 123, "full_name": "acme/widget"},
            },
        }
        self.install_fake_gh(self.metadata)
        self.script_json("sessionctl.py", "init", self.session)
        target = self.script_json(
            "sessionctl.py",
            "snapshot-pr",
            self.session,
            "acme/widget",
            7,
            "--controller-root",
            self.controller,
            env=self.gh_env,
        )
        self.assertEqual(target["head_sha"], self.head)
        checked_target = self.script_json(
            "sessionctl.py", "check-pr", self.session, env=self.gh_env
        )
        self.assertEqual(checked_target["head_sha"], self.head)
        checked_controller = self.script_json(
            "sessionctl.py",
            "check-controller",
            self.session,
            self.controller,
        )
        self.assertEqual(checked_controller["base_sha"], self.base)
        self.script_json(
            "sessionctl.py",
            "patch-session",
            self.session,
            value={"repo": "acme/widget", "cursor": 2},
        )
        self.beats = [
            flag(1, "the value is not pinned", 1, "pin the value"),
            flag(2, "the timeout is fixed", 2, "make the timeout configurable"),
        ]
        for beat in self.beats:
            self.script_json(
                "sessionctl.py", "put-beat", self.session, value=beat
            )

    def accept_offline(self, numbers):
        first, url = self.start_server(self.session)
        _status, state = self.request(url, "/state")
        bodies, actions = [], []
        for number in numbers:
            body = {
                "id": f"review-accept-{number}",
                "session_id": state["session_id"],
                "n": number,
                "action": "accept",
                "note": "include it",
            }
            status, action = self.request(url, "/act", body=body)
            self.assertEqual(status, 200)
            bodies.append(body)
            actions.append(action)
        self.stop_server(first)

        second, url = self.start_server(self.session)
        for body, action in zip(bodies, actions):
            self.assertEqual(self.request(url, "/await")[1], action)
            recovery = self.script_json("sessionctl.py", "reconcile", self.session)
            self.assertEqual(recovery["head"]["seq"], action["seq"])
            self.assertIn(
                body["n"],
                [item["beat_n"] for item in recovery["pending_deliveries"]],
            )
            retry_status, retry = self.request(url, "/act", body=body)
            self.assertEqual(retry_status, 200)
            self.assertEqual(retry, action)
            ack_status, ack = self.request(
                url, "/ack", body={"seq": action["seq"]}
            )
            self.assertEqual(ack_status, 200)
            self.assertEqual(ack, {"handled_seq": action["seq"]})
        self.stop_server(second)
        return actions

    def validated_payload(self, numbers):
        body = self.session / "review-body.txt"
        comments = self.session / "review-comments.json"
        body.write_text("Underwrite findings", encoding="utf-8")
        self.write_json(comments, {
            str(number): {
                "path": "app.py",
                "line": number,
                "side": "RIGHT",
                "body": self.beats[number - 1]["claim"],
            }
            for number in numbers
        })
        self.prepared_intent = self.script_json(
            "sessionctl.py", "prepare-review", self.session,
            "--event", "COMMENT", "--body-file", body, "--comments-file", comments,
        )
        fixed = self.session / "review.fixed.json"
        return fixed, json.loads(fixed.read_text(encoding="utf-8"))

    def check_pr(self, expected=0, require_open=True):
        args = ["check-pr", self.session]
        if require_open:
            args.append("--require-open")
        return self.script(
            "sessionctl.py", *args, env=self.gh_env, expected=expected
        )

    def submit_review(self, *args, expected=0):
        return self.script(
            "sessionctl.py", "submit-review", self.session,
            "--intent", self.prepared_intent["intent_id"], *args,
            env=self.gh_env, expected=expected,
        )

    def persist_review_outcome(self, url, status="complete"):
        patch = {
            "outcome": "GitHub review posted",
            "review_url": url,
            "status": status,
        }
        updated = self.script_json(
            "sessionctl.py", "patch-session", self.session, value=patch
        )
        self.assertEqual(
            {name: updated[name] for name in patch},
            patch,
        )
        return updated

    def move_head(self, sha):
        state = self.fake_gh_state()
        state["metadata"]["head"]["sha"] = sha
        self.write_json(self.gh_state, state)

    def test_two_offline_accepts_land_as_one_exact_review(self):
        actions = self.accept_offline([1, 2])
        recovery = self.script_json("sessionctl.py", "reconcile", self.session)
        self.assertEqual(recovery["handled_seq"], actions[-1]["seq"])
        self.assertEqual(
            [item["beat_n"] for item in recovery["pending_deliveries"]],
            [1, 2],
        )
        self.script("render-report.py", self.session)
        self.script("render-report.py", self.session, "--final", expected=2)

        _fixed, payload = self.validated_payload([1, 2])
        self.assertEqual(payload["commit_id"], self.head)
        self.assertIn("underwrite-review:", payload["body"])
        result = json.loads(self.submit_review().stdout)
        self.assertEqual(result["state"], "landed")
        receipt = result["receipt"]
        self.persist_review_outcome(receipt["url"])
        self.check_pr(require_open=False)
        self.script("render-report.py", self.session, "--final", "--standalone")

        state = self.fake_gh_state()
        self.assertEqual(len(state["post_attempts"]), 1)
        self.assertEqual(state["post_attempts"][0], payload)
        self.assertEqual(len(state["reviews"]), 1)
        self.assertIn(["api", "user", "--jq", ".login"], state["calls"])
        self.assertIn([
            "api", "repos/acme/widget/pulls/7/reviews", "--method", "POST", "--input", "-"
        ], state["calls"])
        self.assertIn([
            "api", "--paginate", "--slurp", "repos/acme/widget/pulls/7/reviews/1/comments"
        ], state["calls"])
        self.submit_review()
        self.assertEqual(len(self.fake_gh_state()["post_attempts"]), 1)
        session = self.script_json("sessionctl.py", "get-session", self.session)
        self.assertEqual(len(session["lands"]), 1)
        for number in (1, 2):
            beat = self.script_json("sessionctl.py", "get-beat", self.session, number)
            self.assertEqual(beat["landed"], receipt["url"])
        page = (self.session / "report.html").read_text(encoding="utf-8")
        self.assertIn(receipt["url"], page)

    def test_a_moved_head_blocks_the_review_post(self):
        self.accept_offline([1])
        fixed, _payload = self.validated_payload([1])
        self.move_head("d" * 40)

        moved = self.submit_review(expected=4)

        self.assertIn("head_sha changed", moved.stderr)
        self.assertEqual(self.fake_gh_state()["post_attempts"], [])
        self.script("render-report.py", self.session, "--final", expected=2)
        self.assertTrue(fixed.exists())
        self.assertTrue(
            self.script_json("sessionctl.py", "reconcile", self.session)[
                "pending_deliveries"
            ]
        )

    def test_a_head_move_during_post_preserves_the_review_receipt(self):
        self.accept_offline([1])
        self.validated_payload([1])
        moved_head = "e" * 40
        self.update_fake_gh(
            move_after_post=True,
            moved_head_sha=moved_head,
        )

        result = json.loads(self.submit_review().stdout)
        receipt = result["receipt"]
        self.persist_review_outcome(receipt["url"])
        moved = self.check_pr(expected=2, require_open=False)

        self.assertIn("head_sha changed", moved.stderr)
        state = self.fake_gh_state()
        self.assertEqual(len(state["post_attempts"]), 1)
        self.assertEqual(len(state["reviews"]), 1)
        self.assertEqual(state["post_attempts"][0]["commit_id"], self.head)
        beat = self.script_json("sessionctl.py", "get-beat", self.session, 1)
        self.assertEqual(beat["landed"], receipt["url"])
        session = self.script_json("sessionctl.py", "get-session", self.session)
        self.assertEqual(session["review_url"], receipt["url"])
        self.assertEqual(session["status"], "complete")

    def test_rejected_post_fails_atomically_then_new_intent_adopts_an_uncertain_post(self):
        self.accept_offline([1])
        self.validated_payload([1])
        failed_intent = self.prepared_intent["intent_id"]
        self.update_fake_gh(post_mode="definite_failure")
        rejected = self.submit_review(expected=1)
        self.assertIn("HTTP 403", rejected.stderr)
        self.assertIn("confirmed no review was created", rejected.stderr)
        recovered = self.script_json("sessionctl.py", "reconcile", self.session)
        self.assertEqual(recovered["failed_deliveries"][0]["beat_n"], 1)

        self.validated_payload([1])
        self.assertNotEqual(self.prepared_intent["intent_id"], failed_intent)
        self.update_fake_gh(post_mode="create_then_fail")
        result = json.loads(self.submit_review().stdout)
        self.assertEqual(result["state"], "landed")
        receipt = result["receipt"]
        self.persist_review_outcome(receipt["url"])
        self.submit_review()
        self.check_pr(require_open=False)
        self.script("render-report.py", self.session, "--final")

        state = self.fake_gh_state()
        self.assertEqual(len(state["post_attempts"]), 2)
        self.assertEqual(len(state["reviews"]), 1)
        self.assertFalse(
            self.script_json("sessionctl.py", "reconcile", self.session)["recovery"]
        )

    def test_rate_limit_stays_submitting_and_moved_target_can_be_retired(self):
        self.accept_offline([1])
        self.validated_payload([1])
        self.update_fake_gh(post_mode="rate_limited")
        uncertain = self.submit_review(expected=3)
        self.assertIn("HTTP 429", uncertain.stderr)
        recovery = self.script_json("sessionctl.py", "reconcile", self.session)
        self.assertEqual(recovery["failed_deliveries"], [])
        self.assertEqual(len(recovery["pending_deliveries"]), 1)
        self.move_head("d" * 40)
        self.submit_review("--retry", expected=4)
        retired = self.script_json(
            "sessionctl.py", "retire-review", self.session,
            "--intent", self.prepared_intent["intent_id"], env=self.gh_env,
        )
        self.assertEqual(retired["state"], "retired")
        self.assertEqual(retired["retirement_reconciliation"]["candidates_examined"], 0)
        self.assertEqual(len(self.fake_gh_state()["post_attempts"]), 1)
        self.assertEqual(self.fake_gh_state()["reviews"], [])


class DistributionInvariants(unittest.TestCase):
    def test_local_worktrees_stay_ignored(self):
        patterns = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

        self.assertIn(".worktrees/", patterns)

    def test_marketplace_plugin_and_entrypoints_stay_aligned(self):
        plugin = json.loads(
            (ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        marketplace = json.loads(
            (ROOT / ".claude-plugin" / "marketplace.json").read_text(
                encoding="utf-8"
            )
        )
        entries = marketplace["plugins"]

        self.assertEqual(plugin["name"], "underwrite")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["name"], plugin["name"])
        self.assertEqual(entries[0]["version"], plugin["version"])
        self.assertEqual(entries[0]["source"], ".")
        skill_root = (ROOT / plugin["skills"]).resolve()
        self.assertEqual(
            sorted(skill_root.glob("*/SKILL.md")),
            [skill_root / "underwrite" / "SKILL.md"],
        )
        for name in (
            "render-report.py",
            "serve.py",
            "sessionctl.py",
            "validate-anchors.py",
        ):
            mode = (skill_root / "underwrite" / "scripts" / name).stat().st_mode
            self.assertTrue(mode & stat.S_IXUSR, name)


if __name__ == "__main__":
    unittest.main()
