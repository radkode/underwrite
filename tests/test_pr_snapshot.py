#!/usr/bin/env python3
"""Exact pull request snapshot and precondition contracts."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "underwrite" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pr_snapshot  # noqa: E402
import session_store  # noqa: E402


class SnapshotCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.remote = self.root / "upstream.git"
        self.work = self.root / "source"
        self.session = self.root / "session"
        self.git("init", "--bare", self.remote)
        self.git("init", self.work)
        self.git("-C", self.work, "config", "user.name", "Underwrite Test")
        self.git("-C", self.work, "config", "user.email", "underwrite@example.test")
        self.git("-C", self.work, "config", "commit.gpgsign", "false")
        (self.work / "common.txt").write_text("common\n", encoding="utf-8")
        self.git("-C", self.work, "add", "common.txt")
        self.git("-C", self.work, "commit", "-m", "common")
        self.common = self.rev("HEAD")
        self.git("-C", self.work, "branch", "-M", "main")
        self.git("-C", self.work, "remote", "add", "origin", self.remote)
        self.git("-C", self.work, "push", "origin", "main")

        self.git("-C", self.work, "checkout", "-b", "feature")
        (self.work / "head.txt").write_text("head only\n", encoding="utf-8")
        self.git("-C", self.work, "add", "head.txt")
        self.git("-C", self.work, "commit", "-m", "head")
        self.head = self.rev("HEAD")
        self.git("-C", self.work, "push", "origin", "HEAD:refs/pull/7/head")

        self.git("-C", self.work, "checkout", "main")
        (self.work / "base-only.txt").write_text("base only\n", encoding="utf-8")
        self.git("-C", self.work, "add", "base-only.txt")
        self.git("-C", self.work, "commit", "-m", "base")
        self.base = self.rev("HEAD")
        self.git("-C", self.work, "push", "origin", "main")
        self.store = session_store.SessionStore(self.session)

    def git(self, *args):
        done = subprocess.run(
            ["git", *map(str, args)], capture_output=True, text=True
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def rev(self, name):
        return self.git("-C", self.work, "rev-parse", name)

    def metadata(self, **changes):
        value = {
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
            "head": {"ref": "main", "sha": self.head, "repo": None},
        }
        for path, replacement in changes.items():
            parent, field = path.split("__", 1) if "__" in path else (None, path)
            if parent is None:
                value[field] = replacement
            else:
                value[parent][field] = replacement
        return value


class Capturing(SnapshotCase):
    def test_capture_uses_the_exact_three_dot_graph_and_fork_pull_ref(self):
        metadata = self.metadata()
        api = mock.Mock(side_effect=[metadata, metadata])

        target = pr_snapshot.capture(self.store, "acme/widget", 7, api=api)

        diff = (self.session / "pr.diff").read_text(encoding="utf-8")
        self.assertIn("head.txt", diff)
        self.assertNotIn("base-only.txt", diff)
        self.assertEqual(target["base_sha"], self.base)
        self.assertEqual(target["head_sha"], self.head)
        self.assertIsNone(target["head_repo_id"])
        self.assertIsNone(target["head_repo"])
        self.assertEqual(target["head_ref"], "main")
        self.assertEqual(target["merge_base_sha"], self.common)
        self.assertEqual(target["changed_files"], 1)
        self.assertIsNone(json.loads((self.session / "pr.json").read_text())["head"]["repo"])
        self.assertEqual(api.call_count, 2)

    def test_a_historical_merged_base_is_fetched_by_exact_sha(self):
        (self.work / "after-merge.txt").write_text("later\n", encoding="utf-8")
        self.git("-C", self.work, "add", "after-merge.txt")
        self.git("-C", self.work, "commit", "-m", "later main")
        self.git("-C", self.work, "push", "origin", "main")
        metadata = self.metadata(state="closed", merged_at="2026-08-26T00:00:00Z")

        target = pr_snapshot.capture(
            self.store, "acme/widget", 7,
            api=mock.Mock(side_effect=[metadata, metadata]),
        )

        self.assertEqual(target["base_sha"], self.base)
        self.assertEqual(target["state"], "closed")
        self.assertNotIn(
            "after-merge.txt", (self.session / "pr.diff").read_text(encoding="utf-8")
        )

    def test_capture_ignores_hostile_diff_configuration(self):
        marker = self.root / "external-ran"
        helper = self.root / "external.sh"
        helper.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
        helper.chmod(0o755)
        config = self.root / "gitconfig"
        config.write_text(
            f"[diff]\n\texternal = {helper}\n\tnoprefix = true\n\tcontext = 0\n",
            encoding="utf-8",
        )
        metadata = self.metadata()

        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config)}):
            pr_snapshot.capture(
                self.store, "acme/widget", 7,
                api=mock.Mock(side_effect=[metadata, metadata]),
            )

        self.assertFalse(marker.exists())
        diff = (self.session / "pr.diff").read_text(encoding="utf-8")
        self.assertIn("--- /dev/null", diff)
        self.assertIn("+++ b/head.txt", diff)

    def test_a_ref_that_moved_after_metadata_is_rejected_without_a_target(self):
        self.git("--git-dir", self.remote, "update-ref", "refs/pull/7/head", self.common)
        metadata = self.metadata()

        with self.assertRaises(pr_snapshot.TargetMoved):
            pr_snapshot.capture(
                self.store, "acme/widget", 7,
                api=mock.Mock(side_effect=[metadata, metadata]),
            )

        self.assertNotIn("target", self.store.snapshot()[0])
        self.assertFalse((self.session / "pr.diff").exists())

    def test_a_final_metadata_move_is_rejected_before_freeze(self):
        first = self.metadata()
        second = self.metadata(head__sha=self.common)

        with self.assertRaises(pr_snapshot.TargetMoved):
            pr_snapshot.capture(
                self.store, "acme/widget", 7,
                api=mock.Mock(side_effect=[first, second]),
            )

        self.assertNotIn("target", self.store.snapshot()[0])
        self.assertFalse((self.session / "pr.diff").exists())

    def test_api_and_local_file_counts_must_agree(self):
        metadata = self.metadata(changed_files=2)
        with self.assertRaisesRegex(pr_snapshot.SnapshotError, "local diff has 1"):
            pr_snapshot.capture(
                self.store, "acme/widget", 7,
                api=mock.Mock(side_effect=[metadata, metadata]),
            )


class Guarding(SnapshotCase):
    def setUp(self):
        super().setUp()
        metadata = self.metadata()
        pr_snapshot.capture(
            self.store, "acme/widget", 7,
            api=mock.Mock(side_effect=[metadata, metadata]),
        )

    def test_check_rejects_head_base_and_lifecycle_drift(self):
        for current, field in (
            (self.metadata(head__sha=self.common), "head_sha"),
            (self.metadata(base__sha=self.common), "base_sha"),
            (self.metadata(state="closed"), "state"),
            (self.metadata(merged_at="2026-08-26T00:00:00Z"), "merged_at"),
            (self.metadata(head__ref="renamed"), "head_ref"),
            (
                self.metadata(
                    head__repo={"id": 99, "full_name": "fork-owner/widget"}
                ),
                "head_repo_id",
            ),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(pr_snapshot.TargetMoved, field):
                    pr_snapshot.check(self.store, api=lambda _repo, _number: current)

    def test_check_rejects_a_same_size_tampered_diff_before_reading_github(self):
        path = self.session / "pr.diff"
        original = path.read_bytes()
        path.write_bytes(b"x" * len(original))
        api = mock.Mock()

        with self.assertRaisesRegex(session_store.Conflict, "does not match"):
            pr_snapshot.check(self.store, api=api)

        api.assert_not_called()

    def test_review_receipt_is_pinned_to_the_frozen_full_head(self):
        response = self.root / "response.json"
        marker = self.store.review_marker()
        response.write_text(
            json.dumps({
                "id": 17,
                "commit_id": self.head,
                "body": f"review\n\n{marker}",
                "user": {"login": "reviewer"},
                "state": "COMMENTED",
                "html_url": "https://example.test/7",
            }),
            encoding="utf-8",
        )
        (self.session / "pr.diff").write_bytes(b"changed after the external effect")
        self.assertEqual(
            pr_snapshot.review_receipt(self.store, response, "reviewer"),
            {
                "commit_id": self.head,
                "marker": marker,
                "review_id": 17,
                "state": "COMMENTED",
                "url": "https://example.test/7",
            },
        )

        response.write_text(
            json.dumps({
                "commit_id": self.head[:12],
                "body": marker,
                "user": {"login": "reviewer"},
                "state": "COMMENTED",
                "html_url": "https://example.test/7",
            }),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(session_store.Conflict, "found 0"):
            pr_snapshot.review_receipt(self.store, response, "reviewer")

    def test_review_recovery_requires_one_marker_actor_and_commit_match(self):
        marker = self.store.review_marker()
        matching = {
            "id": 17,
            "commit_id": self.head,
            "body": marker,
            "user": {"login": "reviewer"},
            "state": "COMMENTED",
            "html_url": "https://example.test/7",
        }
        source = self.root / "reviews.json"
        source.write_text(
            json.dumps([
                [
                    dict(matching, id=10, body="an older review"),
                    dict(matching, id=11, user={"login": "someone-else"}),
                    dict(matching, id=12, state="PENDING"),
                ],
                [matching],
            ]),
            encoding="utf-8",
        )

        receipt = pr_snapshot.review_receipt(self.store, source, "REVIEWER")

        self.assertEqual(receipt["review_id"], 17)
        source.write_text(json.dumps([matching, dict(matching, id=18)]), encoding="utf-8")
        with self.assertRaisesRegex(session_store.Conflict, "found 2"):
            pr_snapshot.review_receipt(self.store, source, "reviewer")

        source.write_text(
            json.dumps([dict(matching, state="DISMISSED")]), encoding="utf-8"
        )
        dismissed = pr_snapshot.review_receipt(self.store, source, "reviewer")
        self.assertEqual(dismissed["state"], "DISMISSED")


class BranchGuarding(SnapshotCase):
    def setUp(self):
        super().setUp()
        self.metadata_value = self.metadata(
            head__ref="feature",
            head__repo={"id": 123, "full_name": "acme/widget"},
        )
        pr_snapshot.capture(
            self.store,
            "acme/widget",
            7,
            api=mock.Mock(side_effect=[self.metadata_value, self.metadata_value]),
        )
        self.store.patch_session({
            "audience": {"mode": "branch", "why": "the author owns the branch"}
        })
        self.git("-C", self.work, "checkout", "feature")

    def accept(self, n=1):
        beat = {
            "n": n,
            "tier": "core",
            "state": "flag",
            "claim": f"finding {n}",
            "where": "head.txt:1",
            "slots": {"what": "x", "proof": "head.txt:1", "risk": "r", "fix": "f"},
        }
        self.store.put_beat(beat)
        return self.store.produce(f"accept-{n}", n, "accept", "yes")

    def commit(self, message):
        path = self.work / f"{message}.txt"
        path.write_text(message + "\n", encoding="utf-8")
        self.git("-C", self.work, "add", path.name)
        self.git("-C", self.work, "commit", "-m", message)
        return self.rev("HEAD")

    def test_worktree_must_start_at_the_frozen_head(self):
        checked = pr_snapshot.check_worktree(self.store, self.work)
        self.assertEqual(checked["head_sha"], self.head)
        self.assertEqual(checked["branch"], "feature")

        unrecorded = self.commit("unrecorded")
        with self.assertRaisesRegex(session_store.Conflict, unrecorded):
            pr_snapshot.check_worktree(self.store, self.work)

    def test_each_landed_commit_must_extend_the_recorded_position_once(self):
        action = self.accept()
        artifact = self.commit("accepted-fix")

        checked = pr_snapshot.check_commit(
            self.store, self.work, action["seq"], 1, artifact, "feature"
        )
        self.assertFalse(checked["replay"])
        self.store.land(action["seq"], 1, artifact, "commit", branch="feature")
        self.assertEqual(
            pr_snapshot.check_worktree(self.store, self.work)["head_sha"], artifact
        )
        self.assertTrue(
            pr_snapshot.check_commit(
                self.store, self.work, action["seq"], 1, artifact, "feature"
            )["replay"]
        )

        self.commit("outside-session")
        with self.assertRaisesRegex(session_store.Conflict, "recorded position"):
            pr_snapshot.check_worktree(self.store, self.work)

    def test_replacement_objects_cannot_spoof_the_commit_parent(self):
        action = self.accept()
        tree = self.git("-C", self.work, "rev-parse", "HEAD^{tree}")
        unrelated = self.git(
            "-C", self.work, "commit-tree", tree, "-p", self.common, "-m", "unrelated"
        )
        replacement = self.git(
            "-C", self.work, "commit-tree", tree, "-p", self.head, "-m", "replacement"
        )
        self.git("-C", self.work, "update-ref", "refs/heads/feature", unrelated)
        self.git("-C", self.work, "replace", unrelated, replacement)

        with self.assertRaisesRegex(session_store.Conflict, "directly on"):
            pr_snapshot.check_commit(
                self.store,
                self.work,
                action["seq"],
                1,
                unrelated,
                "feature",
            )

    def test_grafts_cannot_spoof_the_commit_parent(self):
        action = self.accept()
        tree = self.git("-C", self.work, "rev-parse", "HEAD^{tree}")
        unrelated = self.git(
            "-C", self.work, "commit-tree", tree, "-p", self.common, "-m", "unrelated"
        )
        self.git("-C", self.work, "update-ref", "refs/heads/feature", unrelated)
        grafts = self.work / ".git" / "info" / "grafts"
        grafts.write_text(f"{unrelated} {self.head}\n", encoding="ascii")

        with self.assertRaisesRegex(session_store.Conflict, "directly on"):
            pr_snapshot.check_commit(
                self.store,
                self.work,
                action["seq"],
                1,
                unrelated,
                "feature",
            )

    def test_an_open_deleted_fork_cannot_enter_branch_delivery(self):
        target = dict(self.store.frozen_target())
        target["head_repo_id"] = None
        target["head_repo"] = None
        with mock.patch.object(self.store, "verify_target_files", return_value=target), \
             mock.patch.object(
                 self.store,
                 "branch_position",
                 return_value={
                     "target": target,
                     "expected_head": self.head,
                     "deliveries": [],
                 },
             ):
            with self.assertRaisesRegex(session_store.Conflict, "no head repository"):
                pr_snapshot.check_worktree(self.store, self.work)


class MergedBranchGuarding(SnapshotCase):
    def setUp(self):
        super().setUp()
        metadata = self.metadata(
            state="closed",
            merged_at="2026-08-26T00:00:00Z",
            head__ref="feature",
            head__repo={"id": 123, "full_name": "acme/widget"},
        )
        pr_snapshot.capture(
            self.store,
            "acme/widget",
            7,
            api=mock.Mock(side_effect=[metadata, metadata]),
        )
        self.store.patch_session({
            "audience": {"mode": "branch", "why": "the PR is merged"}
        })
        self.git("-C", self.work, "checkout", "feature")
        self.git("-C", self.work, "branch", "-m", "jacek/fix-merged-pr")

    def test_the_first_fixes_branch_is_pinned_before_editing(self):
        with self.assertRaisesRegex(session_store.Conflict, "has not been pinned"):
            pr_snapshot.check_worktree(self.store, self.work)

        self.store.pin_branch("jacek/fix-merged-pr")

        checked = pr_snapshot.check_worktree(self.store, self.work)
        self.assertEqual(checked["branch"], "jacek/fix-merged-pr")
        with self.assertRaisesRegex(session_store.Conflict, "cannot change"):
            self.store.pin_branch("jacek/another-branch")


if __name__ == "__main__":
    unittest.main()
