#!/usr/bin/env python3
"""Focused contracts for the transactional underwrite session store."""
import importlib.util
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "underwrite" / "scripts"
SPEC = importlib.util.spec_from_file_location("session_store", SCRIPTS / "session_store.py")
session_store = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(session_store)


SESSION = {
    "repo": "acme/widget",
    "cursor": 1,
    "lands": [],
    "audience": {"mode": "branch", "why": "the author owns the branch"},
}
FLAG = {
    "n": 1,
    "tier": "core",
    "state": "flag",
    "claim": "unpinned",
    "where": "a.py:1",
    "slots": {"what": "x", "proof": "a.py:1", "risk": "r", "fix": "pin it"},
}
CLEAN = {
    "n": 2,
    "tier": "core",
    "state": "clean",
    "claim": "sound",
    "where": "b.py:1",
    "slots": {"what": "y", "proof": "b.py:1"},
}


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.write_legacy(SESSION, [FLAG, CLEAN])
        self.store = session_store.SessionStore(self.root)

    def write_legacy(self, session, beats):
        (self.root / "beats").mkdir(exist_ok=True)
        (self.root / "session.json").write_text(json.dumps(session), encoding="utf-8")
        for beat in beats:
            (self.root / "beats" / f"{beat['n']:02d}.json").write_text(
                json.dumps(beat), encoding="utf-8"
            )

    def beat(self, n):
        return next(beat for beat in self.store.snapshot()[1] if beat["n"] == n)


class FrozenTargets(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.store = session_store.SessionStore(self.root)

    def target(self, suffix="1"):
        return {
            "version": 1,
            "kind": "github_pr",
            "repo": "acme/widget",
            "number": 7,
            "state": "open",
            "merged_at": None,
            "base_sha": ("a" if suffix == "1" else "c") * 40,
            "head_sha": ("b" if suffix == "1" else "d") * 40,
            "head_repo_id": 123,
            "head_repo": "acme/widget",
            "head_ref": "feature",
            "merge_base_sha": ("e" if suffix == "1" else "f") * 40,
            "changed_files": 1,
        }

    def inputs(self, suffix="1"):
        diff = self.root / f"capture-{suffix}.diff"
        metadata = self.root / f"capture-{suffix}.json"
        diff.write_bytes(f"diff {suffix}\n".encode())
        metadata.write_text(json.dumps({"capture": suffix}), encoding="utf-8")
        return diff, metadata

    def context_input(self, suffix="1", document=None):
        if document is None:
            document = {
                "version": 1,
                "base_sha": self.target(suffix)["base_sha"],
                "files": [],
            }
        path = self.root / f"trusted-context-{suffix}.json"
        path.write_text(
            json.dumps(
                document,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def freeze(self, suffix="1"):
        diff, metadata = self.inputs(suffix)
        return self.store.freeze_target(self.target(suffix), diff, metadata)

    def set_historical_audience(self, mode):
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            row = db.execute(
                "SELECT body_json FROM session WHERE singleton = 1"
            ).fetchone()
            body = json.loads(row[0])
            body["audience"] = {"mode": mode, "why": "historical session"}
            db.execute(
                "UPDATE session SET body_json = ? WHERE singleton = 1",
                (json.dumps(body),),
            )

    def test_freeze_records_one_hash_verified_target(self):
        target = self.freeze()

        self.assertEqual(self.store.snapshot()[0]["target"], target)
        self.assertEqual(self.store.verify_target_files(), target)
        self.assertEqual((self.root / "pr.diff").read_bytes(), b"diff 1\n")
        self.assertEqual(json.loads((self.root / "pr.json").read_text()), {"capture": "1"})

    def test_generic_mutations_cannot_add_remove_or_change_a_target(self):
        diff, metadata = self.inputs()
        with self.assertRaisesRegex(session_store.Conflict, "freeze-target"):
            self.store.put_session(dict(SESSION, target={
                **self.target(),
                "diff_sha256": "0" * 64,
                "diff_bytes": 0,
            }))

        target = self.store.freeze_target(self.target(), diff, metadata)
        self.store.freeze_execution("no_exec")
        with self.assertRaisesRegex(session_store.Conflict, "cannot change"):
            self.store.put_session(SESSION)
        with self.assertRaisesRegex(session_store.Conflict, "freeze-target"):
            self.store.patch_session({"target": target})

        action = self.store.produce("nav-1", None, "next", "")
        without_target = dict(self.store.snapshot()[0])
        without_target.pop("target")
        without_target.pop("execution_policy")
        with self.assertRaisesRegex(session_store.StoreError, "frozen PR target"):
            self.store.apply(action["seq"], {"kind": "walk"}, session=without_target)
        self.assertEqual(self.store.head()["state"], "produced")

    def test_first_freeze_is_refused_after_the_walk_starts(self):
        self.store.put_beat(FLAG)
        diff, metadata = self.inputs()

        with self.assertRaisesRegex(session_store.Conflict, "before beats or actions"):
            self.store.freeze_target(self.target(), diff, metadata)

        self.assertNotIn("target", self.store.snapshot()[0])
        self.assertFalse((self.root / "pr.diff").exists())

    def test_target_audience_and_no_exec_policy_are_frozen_atomically(self):
        target = self.freeze()

        session = self.store.snapshot()[0]
        policy = session["execution_policy"]
        self.assertEqual(session["audience"]["mode"], "review")
        self.assertEqual(policy["trust"], "untrusted")
        self.assertEqual(policy["mode"], "no_exec")
        self.assertEqual(policy["target"]["head_sha"], target["head_sha"])
        self.store.put_beat(FLAG)
        self.store.produce("nav-1", None, "next", "")

    def test_non_open_prs_freeze_report_audience(self):
        cases = (
            ("closed", None),
            ("closed", "2026-08-26T00:00:00Z"),
        )
        for index, (state, merged_at) in enumerate(cases, 1):
            with self.subTest(state=state, merged_at=merged_at):
                root = self.root / f"case-{index}"
                store = session_store.SessionStore(root)
                target = dict(
                    self.target(), state=state, merged_at=merged_at
                )
                diff = self.root / f"case-{index}.diff"
                metadata = self.root / f"case-{index}.json"
                context = self.root / f"case-{index}-context.json"
                diff.write_text("diff\n", encoding="utf-8")
                metadata.write_text("{}\n", encoding="utf-8")
                context.write_text(
                    json.dumps({
                        "version": 1,
                        "base_sha": target["base_sha"],
                        "files": [],
                    }, separators=(",", ":"), sort_keys=True) + "\n",
                    encoding="utf-8",
                )

                store.freeze_target(target, diff, metadata, context)

                self.assertEqual(store.snapshot()[0]["audience"]["mode"], "report")

    def test_report_audience_requires_a_frozen_pr(self):
        with self.assertRaisesRegex(
            session_store.StoreError, "frozen PR target"
        ):
            self.store.put_session({
                **SESSION,
                "audience": {"mode": "report", "why": "bypass delivery"},
            })

    def test_review_audience_requires_a_frozen_or_legacy_pr(self):
        with self.assertRaisesRegex(
            session_store.StoreError, "frozen PR target or legacy PR marker"
        ):
            self.store.put_session({
                **SESSION,
                "audience": {"mode": "review", "why": "bypass delivery"},
            })

    def test_report_audience_cannot_carry_lands(self):
        target = dict(self.target(), state="closed", merged_at=None)
        diff, metadata = self.inputs()
        self.store.freeze_target(
            target, diff, metadata, self.context_input()
        )
        session = self.store.snapshot()[0]
        session["lands"] = [{
            "state": "ready",
            "what": "external work",
            "where": "somewhere else",
        }]

        with self.assertRaisesRegex(
            session_store.StoreError, "report audience cannot have lands"
        ):
            self.store.put_session(session)

    def test_freeze_overrides_a_prefreeze_pr_audience(self):
        self.store.put_session(dict(SESSION, delivery_branch="stale"))
        diff, metadata = self.inputs()

        self.store.freeze_target(
            self.target(), diff, metadata, self.context_input()
        )

        self.assertEqual(self.store.snapshot()[0]["audience"]["mode"], "review")
        self.assertNotIn("delivery_branch", self.store.snapshot()[0])

    def test_exact_replay_preserves_a_historical_audience(self):
        diff, metadata = self.inputs()
        target = self.store.freeze_target(
            self.target(), diff, metadata, self.context_input()
        )
        self.set_historical_audience("branch")

        diff, metadata = self.inputs()
        replay = self.store.freeze_target(
            self.target(), diff, metadata, self.context_input()
        )

        self.assertEqual(replay, target)
        self.assertEqual(self.store.snapshot()[0]["audience"]["mode"], "branch")
        self.store.put_beat(FLAG)
        with self.assertRaisesRegex(session_store.Conflict, "frozen lifecycle"):
            self.store.produce("accept-1", 1, "accept", "yes")

    def test_report_accept_is_terminal_and_freezes_agent_content(self):
        target = dict(self.target(), state="closed", merged_at=None)
        diff, metadata = self.inputs()
        self.store.freeze_target(
            target, diff, metadata, self.context_input()
        )
        self.store.put_beat(FLAG)

        action = self.store.produce("accept-1", 1, "accept", "include it")

        self.assertEqual(action["result"]["delivery"], "none")
        self.assertEqual(
            self.store.presentation_snapshot()[1][0]["delivery"]["state"],
            "none",
        )
        with self.assertRaisesRegex(session_store.Conflict, "cannot change slots"):
            changed = self.store.snapshot()[1][0]
            changed["slots"]["proof"] = "changed.py:1"
            self.store.put_beat(changed)
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            db.execute(
                "UPDATE beats SET delivery_state = 'pending', delivery_json = ? "
                "WHERE n = 1",
                (json.dumps({"kind": "review", "cause_seq": action["seq"]}),),
            )
        with self.assertRaisesRegex(session_store.Conflict, "has not landed"):
            self.store.ack(action["seq"])
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            db.execute(
                "UPDATE beats SET delivery_state = 'none', delivery_json = NULL "
                "WHERE n = 1"
            )
        self.assertEqual(self.store.ack(action["seq"]), {"handled_seq": 1})
        self.assertFalse(self.store.reconcile()["recovery"])
        with self.assertRaisesRegex(session_store.Conflict, "does not match"):
            self.store.land(action["seq"], 1, "review-url", "review")
        with self.assertRaisesRegex(session_store.Conflict, "no external delivery"):
            self.store.fail(action["seq"], "failed", "retry")

    def test_report_accept_requires_shippable_frozen_content(self):
        target = dict(self.target(), state="closed", merged_at=None)
        diff, metadata = self.inputs()
        self.store.freeze_target(
            target, diff, metadata, self.context_input()
        )
        invalid = dict(FLAG)
        invalid["slots"] = dict(FLAG["slots"], proof="trust me")
        self.store.put_beat(invalid)

        with self.assertRaisesRegex(
            session_store.Conflict, "report acceptance requires a shippable beat"
        ):
            self.store.produce("accept-1", 1, "accept", "include it")

        self.assertEqual(self.store.snapshot()[1][0]["state"], "flag")
        self.assertIsNone(self.store.head())

    def test_report_accept_ack_requires_the_accepted_beat(self):
        target = dict(self.target(), state="closed", merged_at=None)
        diff, metadata = self.inputs()
        self.store.freeze_target(
            target, diff, metadata, self.context_input()
        )
        self.store.put_beat(FLAG)
        action = self.store.produce("accept-1", 1, "accept", "include it")
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            row = db.execute(
                "SELECT body_json FROM beats WHERE n = 1"
            ).fetchone()
            body = json.loads(row[0])
            body["state"] = "flag"
            db.execute(
                "UPDATE beats SET body_json = ? WHERE n = 1",
                (json.dumps(body),),
            )

        with self.assertRaisesRegex(
            session_store.Conflict, "no longer resolves to an accepted beat"
        ):
            self.store.ack(action["seq"])

    def test_report_accept_keeps_reviewer_notes_mutable(self):
        target = dict(self.target(), state="closed", merged_at=None)
        diff, metadata = self.inputs()
        self.store.freeze_target(
            target, diff, metadata, self.context_input()
        )
        self.store.put_beat(FLAG)
        accepted = self.store.produce("accept-1", 1, "accept", "include it")
        self.store.ack(accepted["seq"])

        noted = self.store.produce("note-1", 1, "note", "reviewer follow-up")
        self.store.ack(noted["seq"])

        self.assertEqual(self.store.snapshot()[1][0]["call"], "reviewer follow-up")

    def test_report_accept_survives_export_and_reimport_without_delivery(self):
        target = dict(
            self.target(), state="closed", merged_at="2026-08-26T00:00:00Z"
        )
        diff, metadata = self.inputs()
        self.store.freeze_target(
            target, diff, metadata, self.context_input()
        )
        self.store.put_beat(FLAG)
        action = self.store.produce("accept-1", 1, "accept", "include it")
        self.store.ack(action["seq"])
        self.store.export_json()
        (self.root / "session.sqlite3").unlink()

        restored = session_store.SessionStore(self.root)

        beat = restored.presentation_snapshot()[1][0]
        self.assertEqual(beat["state"], "accepted")
        self.assertEqual(beat["delivery"]["state"], "none")
        self.assertFalse(restored.reconcile()["recovery"])

    def test_invalid_accepted_report_cannot_be_reimported(self):
        target = dict(self.target(), state="closed", merged_at=None)
        diff, metadata = self.inputs()
        self.store.freeze_target(
            target, diff, metadata, self.context_input()
        )
        self.store.put_beat(FLAG)
        action = self.store.produce("accept-1", 1, "accept", "include it")
        self.store.ack(action["seq"])
        self.store.export_json()
        path = self.root / "beats" / "01.json"
        exported = json.loads(path.read_text(encoding="utf-8"))
        exported["slots"]["proof"] = "trust me"
        path.write_text(json.dumps(exported), encoding="utf-8")
        (self.root / "session.sqlite3").unlink()

        with self.assertRaisesRegex(
            session_store.MigrationError, "accepted report beat 1 is not shippable"
        ):
            session_store.SessionStore(self.root)

    def test_execution_policy_is_target_bound_write_once_and_idempotent(self):
        diff, metadata = self.inputs()
        target = self.store.freeze_target(
            self.target(), diff, metadata, self.context_input()
        )
        before = self.store.delivery_state()["render_revision"]
        policy = self.store.freeze_execution("no_exec")
        after = self.store.delivery_state()["render_revision"]

        self.assertEqual(policy, {
            "version": 1,
            "trust": "untrusted",
            "mode": "no_exec",
            "target": {
                "repo": target["repo"],
                "number": target["number"],
                "base_sha": target["base_sha"],
                "head_sha": target["head_sha"],
                "diff_sha256": target["diff_sha256"],
                "trusted_context_sha256": target["trusted_context_sha256"],
            },
        })
        self.assertEqual(after, before)
        self.assertEqual(self.store.freeze_execution("no_exec"), policy)
        self.assertEqual(self.store.delivery_state()["render_revision"], after)
        with self.assertRaisesRegex(session_store.StoreError, "one of no_exec"):
            self.store.freeze_execution("sandboxed")
        with self.assertRaisesRegex(session_store.Conflict, "freeze-execution"):
            self.store.patch_session({"execution_policy": policy})
        without_policy = dict(self.store.snapshot()[0])
        without_policy.pop("execution_policy")
        with self.assertRaisesRegex(session_store.Conflict, "cannot change"):
            self.store.put_session(without_policy)

    def test_target_code_execution_is_not_a_supported_policy(self):
        self.freeze()

        with self.assertRaisesRegex(session_store.StoreError, "one of no_exec"):
            self.store.freeze_execution("sandboxed")

        policy = self.store.freeze_execution("no_exec")
        self.assertEqual(policy["mode"], "no_exec")
        with self.assertRaisesRegex(session_store.Conflict, "forbids"):
            self.store.check_execution()

    def test_the_frozen_diff_is_read_through_the_store_and_hash_verified(self):
        """SKILL.md says to inspect the frozen diff through Underwrite's own tools, and
        read-blob covered only the blobs; opening pr.diff by hand was the only way."""
        diff, metadata = self.inputs()
        target = self.store.freeze_target(self.target(), diff, metadata)

        read = self.store.read_diff()
        self.assertEqual(read["sha256"], target["diff_sha256"])
        self.assertEqual(read["bytes"], target["diff_bytes"])
        self.assertEqual(read["encoding"], "utf-8")
        self.assertEqual(
            read["content"], (self.root / "pr.diff").read_text(encoding="utf-8")
        )

    def test_reading_the_diff_refuses_a_projection_that_no_longer_matches(self):
        diff, metadata = self.inputs()
        self.store.freeze_target(self.target(), diff, metadata)

        (self.root / "pr.diff").write_text("not the frozen diff\n", encoding="utf-8")
        with self.assertRaisesRegex(session_store.Conflict, "does not match"):
            self.store.read_diff()

        (self.root / "pr.diff").unlink()
        with self.assertRaisesRegex(session_store.Conflict, "is missing"):
            self.store.read_diff()

    def test_reading_the_diff_fails_closed_above_max_bytes(self):
        diff, metadata = self.inputs()
        target = self.store.freeze_target(self.target(), diff, metadata)

        with self.assertRaisesRegex(session_store.Conflict, "above max_bytes"):
            self.store.read_diff(max_bytes=target["diff_bytes"] - 1)
        self.assertEqual(
            self.store.read_diff(max_bytes=target["diff_bytes"])["bytes"],
            target["diff_bytes"],
        )
        with self.assertRaisesRegex(session_store.StoreError, "diff max_bytes"):
            self.store.read_diff(max_bytes=0)

    def test_trusted_context_is_semantically_validated_and_hash_verified(self):
        content = "review from the frozen base\n"
        encoded = content.encode("utf-8")
        blob_sha = hashlib.sha1(
            b"blob " + str(len(encoded)).encode("ascii") + b"\0" + encoded
        ).hexdigest()
        context = {
            "version": 1,
            "base_sha": self.target()["base_sha"],
            "files": [{
                "path": "AGENTS.md",
                "mode": "100644",
                "blob_sha": blob_sha,
                "content": content,
            }],
        }
        diff, metadata = self.inputs()
        target = self.store.freeze_target(
            self.target(), diff, metadata, self.context_input(document=context)
        )

        self.assertEqual(self.store.read_trusted_context(), context)
        policy = self.store.freeze_execution("no_exec")
        with self.assertRaisesRegex(session_store.Conflict, "forbids"):
            self.store.check_execution()
        self.assertEqual(
            policy["target"]["trusted_context_sha256"],
            target["trusted_context_sha256"],
        )

        (self.root / "trusted-context.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(session_store.Conflict, "does not match"):
            self.store.read_trusted_context()
        with self.assertRaisesRegex(session_store.Conflict, "one exact regular file"):
            self.store.check_execution()

    def test_trusted_context_rejects_a_governing_symlink(self):
        content = "rules.md"
        encoded = content.encode("utf-8")
        blob_sha = hashlib.sha1(
            b"blob " + str(len(encoded)).encode("ascii") + b"\0" + encoded
        ).hexdigest()
        context = {
            "version": 1,
            "base_sha": self.target()["base_sha"],
            "files": [{
                "path": "AGENTS.md",
                "mode": "120000",
                "blob_sha": blob_sha,
                "content": content,
            }],
        }
        diff, metadata = self.inputs()

        with self.assertRaisesRegex(session_store.StoreError, "regular file"):
            self.store.freeze_target(
                self.target(), diff, metadata, self.context_input(document=context)
            )

        self.assertNotIn("target", self.store.snapshot()[0])

    def test_a_context_for_a_different_base_is_never_frozen(self):
        context = {
            "version": 1,
            "base_sha": "f" * 40,
            "files": [],
        }
        diff, metadata = self.inputs()

        with self.assertRaisesRegex(session_store.StoreError, "base_sha"):
            self.store.freeze_target(
                self.target(), diff, metadata, self.context_input(document=context)
            )

        self.assertNotIn("target", self.store.snapshot()[0])
        self.assertFalse((self.root / "trusted-context.json").exists())

    def test_open_pr_uses_review_without_relaxing_no_exec(self):
        diff, metadata = self.inputs()
        self.store.freeze_target(
            self.target(), diff, metadata, self.context_input()
        )
        policy = self.store.freeze_execution("no_exec")
        self.store.put_beat(FLAG)

        with self.assertRaisesRegex(session_store.Conflict, "target is frozen"):
            self.store.patch_session({"audience": {"mode": "branch"}})
        accepted = self.store.produce("accept-1", 1, "accept", "include it")
        self.assertEqual(self.store.ack(accepted["seq"]), {"handled_seq": 1})
        self.assertTrue(self.store.delivery_state()["recovery"])
        landed = self.store.land(
            accepted["seq"], 1, "https://example.test/review/1", "review"
        )
        self.assertEqual(landed["kind"], "review")
        self.assertFalse(self.store.delivery_state()["recovery"])
        session = self.store.snapshot()[0]
        self.assertEqual(session["audience"]["mode"], "review")
        self.assertEqual(session["execution_policy"], policy)

    def test_a_v3_pr_without_trusted_context_requires_replacement(self):
        self.freeze()
        self.store.freeze_execution("no_exec")
        self.store.put_beat(FLAG)
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            row = db.execute(
                "SELECT body_json FROM session WHERE singleton = 1"
            ).fetchone()
            body = json.loads(row[0])
            body.pop("execution_policy")
            db.execute(
                "UPDATE session SET body_json = ? WHERE singleton = 1",
                (json.dumps(body),),
            )
            db.execute("PRAGMA user_version = 3")

        self.store = session_store.SessionStore(self.root)

        self.assertEqual(
            self.store.snapshot()[0]["execution_policy"]["mode"], "no_exec"
        )
        with self.assertRaisesRegex(
            session_store.Conflict, "no frozen trusted context.*replacement"
        ):
            self.store.produce("accept-1", 1, "accept", "include it")

    def test_a_v3_contextless_review_delivery_requires_replacement(self):
        self.freeze()
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            row = db.execute(
                "SELECT body_json FROM session WHERE singleton = 1"
            ).fetchone()
            body = json.loads(row[0])
            body.pop("execution_policy")
            db.execute(
                "UPDATE session SET body_json = ? WHERE singleton = 1",
                (json.dumps(body),),
            )
        with mock.patch.object(
            session_store, "EXECUTION_MODES", ("sandboxed", "no_exec")
        ), mock.patch.object(
            session_store.SessionStore, "_replacement_reason", return_value=None
        ):
            self.store.freeze_execution("sandboxed")
            self.store.put_beat(FLAG)
            action = self.store.produce("accept-1", 1, "accept", "include it")
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            db.execute("PRAGMA user_version = 3")

        upgraded = session_store.SessionStore(self.root)
        pending = upgraded.reconcile()["pending_deliveries"]

        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0]["blocked"])
        self.assertIn("do not perform external delivery", pending[0]["blocked_reason"])
        with self.assertRaisesRegex(session_store.Conflict, "supervised replacement"):
            upgraded.land(
                action["seq"], 1, "https://example.test/review/1", "review"
            )

    def test_v3_pr_sessions_discard_claimed_sandboxing_and_block_new_commit_land(self):
        diff, metadata = self.inputs()
        self.store.freeze_target(
            self.target(), diff, metadata, self.context_input()
        )
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            row = db.execute(
                "SELECT body_json FROM session WHERE singleton = 1"
            ).fetchone()
            body = json.loads(row[0])
            body.pop("execution_policy")
            db.execute(
                "UPDATE session SET body_json = ? WHERE singleton = 1",
                (json.dumps(body),),
            )
        with mock.patch.object(
            session_store, "EXECUTION_MODES", ("sandboxed", "no_exec")
        ), mock.patch.object(
            session_store.SessionStore, "_replacement_reason", return_value=None
        ):
            self.store.freeze_execution("sandboxed")
            self.set_historical_audience("branch")
            self.store.put_beat(FLAG)
            action = self.store.produce("accept-1", 1, "accept", "yes")
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            db.execute("PRAGMA user_version = 3")

        self.store = session_store.SessionStore(self.root)

        self.assertEqual(
            self.store.snapshot()[0]["execution_policy"]["mode"], "no_exec"
        )
        pending = self.store.reconcile()["pending_deliveries"]
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0]["blocked"])
        self.assertIn("frozen lifecycle", pending[0]["blocked_reason"])
        with self.assertRaisesRegex(session_store.Conflict, "supervised replacement"):
            self.store.land(action["seq"], 1, "c" * 40, "commit", branch="feature")

    def test_v3_applied_navigation_replay_uses_the_migrated_execution_policy(self):
        diff, metadata = self.inputs()
        self.store.freeze_target(
            self.target(), diff, metadata, self.context_input()
        )
        action = self.store.produce("nav-1", None, "next", "")
        moved = dict(self.store.snapshot()[0], cursor=2, current_beat=2)
        original_v3_session = dict(moved)
        original_v3_session.update({
            "execution_policy": {"mode": "user metadata"},
            "legacy_pr": {"repo": "other/project", "number": 99},
        })
        result = {"kind": "walk", "cursor": 2, "current_beat": 2, "tier": "core"}
        applied = self.store.apply(action["seq"], result, session=moved)

        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            row = db.execute(
                "SELECT body_json FROM session WHERE singleton = 1"
            ).fetchone()
            body = json.loads(row[0])
            body.update({
                "execution_policy": {"mode": "user metadata"},
                "legacy_pr": {"repo": "other/project", "number": 99},
            })
            db.execute(
                "UPDATE session SET body_json = ? WHERE singleton = 1",
                (json.dumps(body),),
            )
            row = db.execute(
                "SELECT result_json FROM actions WHERE seq = ?", (action["seq"],)
            ).fetchone()
            application = json.loads(row[0])
            application["session"] = dict(original_v3_session)
            db.execute(
                "UPDATE actions SET result_json = ? WHERE seq = ?",
                (json.dumps(application), action["seq"]),
            )
            db.execute("PRAGMA user_version = 3")

        self.store = session_store.SessionStore(self.root)
        current, _beats = self.store.snapshot()

        self.assertEqual(
            self.store.apply(
                action["seq"], result, session=original_v3_session
            ),
            applied,
        )
        self.assertEqual(
            self.store.apply(action["seq"], result, session=current), applied
        )
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            row = db.execute(
                "SELECT result_json FROM actions WHERE seq = ?", (action["seq"],)
            ).fetchone()
        application = json.loads(row[0])
        self.assertEqual(
            application["session"]["execution_policy"],
            current["execution_policy"],
        )

    def test_v3_produced_navigation_uses_the_migrated_execution_policy(self):
        diff, metadata = self.inputs()
        self.store.freeze_target(
            self.target(), diff, metadata, self.context_input()
        )
        first = self.store.produce("nav-1", None, "next", "")
        second = self.store.produce("nav-2", None, "back", "")
        original = dict(self.store.snapshot()[0])
        original.pop("execution_policy")
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            row = db.execute(
                "SELECT body_json FROM session WHERE singleton = 1"
            ).fetchone()
            body = json.loads(row[0])
            body.pop("execution_policy")
            db.execute(
                "UPDATE session SET body_json = ? WHERE singleton = 1",
                (json.dumps(body),),
            )
            db.execute("PRAGMA user_version = 3")

        self.store = session_store.SessionStore(self.root)
        moved = dict(original, cursor=2, current_beat=2)
        result = {"kind": "walk", "cursor": 2, "current_beat": 2}
        self.assertEqual(
            self.store.apply(first["seq"], result, session=moved)["state"],
            "applied",
        )
        self.store.ack(first["seq"])
        current, beats = self.store.snapshot()
        current.pop("execution_policy")
        self.assertEqual(
            self.store.reconcile_action(
                second["seq"],
                result,
                session=current,
                beats=beats,
                evidence="the migrated target session is already on disk",
            )["state"],
            "applied",
        )

    def test_no_exec_migration_keeps_an_exact_landed_commit_replay_idempotent(self):
        diff, metadata = self.inputs()
        self.store.freeze_target(
            self.target(), diff, metadata, self.context_input()
        )
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            row = db.execute(
                "SELECT body_json FROM session WHERE singleton = 1"
            ).fetchone()
            body = json.loads(row[0])
            body.pop("execution_policy")
            db.execute(
                "UPDATE session SET body_json = ? WHERE singleton = 1",
                (json.dumps(body),),
            )
        with mock.patch.object(
            session_store, "EXECUTION_MODES", ("sandboxed", "no_exec")
        ), mock.patch.object(
            session_store.SessionStore, "_replacement_reason", return_value=None
        ):
            self.store.freeze_execution("sandboxed")
            self.set_historical_audience("branch")
            self.store.put_beat(FLAG)
            action = self.store.produce("accept-1", 1, "accept", "yes")
            landed = self.store.land(
                action["seq"], 1, "c" * 40, "commit", branch="feature"
            )
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            row = db.execute(
                "SELECT body_json FROM session WHERE singleton = 1"
            ).fetchone()
            body = json.loads(row[0])
            body.pop("execution_policy")
            db.execute(
                "UPDATE session SET body_json = ? WHERE singleton = 1",
                (json.dumps(body),),
            )
            db.execute("PRAGMA user_version = 3")

        self.store = session_store.SessionStore(self.root)

        self.assertEqual(
            self.store.land(
                action["seq"], 1, "c" * 40, "commit", branch="feature"
            ),
            landed,
        )

    def test_a_delivery_branch_is_write_once_through_its_gateway(self):
        self.freeze()
        self.set_historical_audience("branch")

        pinned = self.store.pin_branch("feature")

        self.assertEqual(pinned["delivery_branch"], "feature")
        self.assertEqual(self.store.pin_branch("feature"), pinned)
        with self.assertRaisesRegex(session_store.Conflict, "cannot change"):
            self.store.pin_branch("another")
        with self.assertRaisesRegex(session_store.Conflict, "pin-branch"):
            self.store.patch_session({"delivery_branch": "another"})
        without_branch = dict(self.store.snapshot()[0])
        without_branch.pop("delivery_branch")
        with self.assertRaisesRegex(session_store.Conflict, "cannot change"):
            self.store.put_session(without_branch)

    def test_exact_replay_repairs_a_corrupt_projection(self):
        target = self.freeze()
        (self.root / "pr.diff").write_bytes(b"tampered")
        with self.assertRaisesRegex(session_store.Conflict, "one exact regular file"):
            self.store.verify_target_files()

        replay = self.freeze()

        self.assertEqual(replay, target)
        self.assertEqual(self.store.verify_target_files(), target)

    def test_verified_diff_returns_the_exact_hashed_bytes(self):
        target = self.freeze()

        read_target, data = self.store.read_verified_target_diff()
        (self.root / "pr.diff").write_bytes(b"changed\n")

        self.assertEqual(read_target, target)
        self.assertEqual(data, b"diff 1\n")
        with self.assertRaisesRegex(session_store.Conflict, "does not match"):
            self.store.read_verified_target_diff()

    def test_object_bundle_is_hash_bound_and_verified_on_every_read(self):
        diff, metadata = self.inputs()
        bundle = self.root / "captured.bundle"
        bundle.write_bytes(b"exact Git objects\n")

        target = self.store.freeze_target(
            self.target(), diff, metadata, None, bundle
        )

        self.assertEqual(self.store.read_object_bundle(), b"exact Git objects\n")
        copied = self.root / "verified-copy.bundle"
        self.assertEqual(self.store.copy_verified_object_bundle(copied), target)
        self.assertEqual(copied.read_bytes(), b"exact Git objects\n")
        with mock.patch.object(
            session_store, "MAX_OBJECT_BUNDLE_MEMORY_BYTES", 1
        ):
            with self.assertRaisesRegex(session_store.Conflict, "too large"):
                self.store.read_object_bundle()
        self.assertEqual(
            target["object_bundle_sha256"],
            hashlib.sha256(b"exact Git objects\n").hexdigest(),
        )
        self.assertEqual(target["object_bundle_bytes"], 18)
        self.assertEqual(
            self.store.snapshot()[0]["execution_policy"]["target"][
                "object_bundle_sha256"
            ],
            target["object_bundle_sha256"],
        )

        (self.root / "pr.bundle").write_bytes(b"tampered\n")
        bad_copy = self.root / "bad-copy.bundle"
        with self.assertRaisesRegex(session_store.Conflict, "does not match"):
            self.store.copy_verified_object_bundle(bad_copy)
        self.assertFalse(bad_copy.exists())
        with self.assertRaisesRegex(session_store.Conflict, "one exact regular file"):
            self.store.read_object_bundle()
        with self.assertRaisesRegex(session_store.Conflict, "one exact regular file"):
            self.store.verify_target_files()

    def test_concurrent_different_freezes_cannot_split_identity_and_diff(self):
        first = self.inputs("1")
        second = self.inputs("2")
        ready = threading.Barrier(2)
        results, errors = [], []

        def freeze(target, inputs):
            ready.wait()
            try:
                results.append(self.store.freeze_target(target, *inputs))
            except session_store.Conflict as error:
                errors.append(error)

        threads = [
            threading.Thread(target=freeze, args=(self.target("1"), first)),
            threading.Thread(target=freeze, args=(self.target("2"), second)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)

        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        frozen = self.store.verify_target_files()
        expected = b"diff 1\n" if frozen["base_sha"] == "a" * 40 else b"diff 2\n"
        self.assertEqual((self.root / "pr.diff").read_bytes(), expected)

    def test_target_publication_serializes_with_the_first_beat(self):
        diff, metadata = self.inputs()
        publishing = threading.Event()
        release = threading.Event()
        beat_started = threading.Event()
        frozen, freeze_errors, beat_errors = [], [], []
        replace = session_store.os.replace

        def paused_replace(source, destination):
            if Path(destination) == self.root / "pr.json":
                publishing.set()
                if not release.wait(5):
                    raise RuntimeError("timed out waiting to publish target")
            return replace(source, destination)

        def freeze():
            try:
                frozen.append(
                    self.store.freeze_target(self.target(), diff, metadata)
                )
            except Exception as error:
                freeze_errors.append(error)

        def add_beat():
            beat_started.set()
            try:
                self.store.put_beat(FLAG)
            except Exception as error:
                beat_errors.append(error)

        with mock.patch.object(session_store.os, "replace", paused_replace):
            freeze_thread = threading.Thread(target=freeze)
            freeze_thread.start()
            self.assertTrue(publishing.wait(5))

            beat_thread = threading.Thread(target=add_beat)
            beat_thread.start()
            self.assertTrue(beat_started.wait(5))
            beat_thread.join(0.5)
            beat_was_blocked = beat_thread.is_alive()

            release.set()
            freeze_thread.join(5)
            beat_thread.join(5)

        self.assertTrue(beat_was_blocked)
        self.assertFalse(freeze_thread.is_alive())
        self.assertFalse(beat_thread.is_alive())
        self.assertEqual(freeze_errors, [])
        self.assertEqual(len(frozen), 1)
        self.assertEqual(beat_errors, [])
        self.assertEqual(self.store.snapshot()[1][0]["n"], 1)
        self.assertEqual(
            self.store.snapshot()[0]["execution_policy"]["mode"], "no_exec"
        )
        self.assertEqual(self.store.verify_target_files(), frozen[0])

    def test_a_failed_database_save_publishes_no_target_and_retry_repairs_it(self):
        diff, metadata = self.inputs()
        with mock.patch.object(
            self.store, "_save_session", side_effect=OSError("database unavailable")
        ):
            with self.assertRaisesRegex(OSError, "database unavailable"):
                self.store.freeze_target(self.target(), diff, metadata)

        self.assertNotIn("target", self.store.snapshot()[0])
        target = self.store.freeze_target(self.target(), diff, metadata)
        self.assertEqual(self.store.verify_target_files(), target)


class CreatingAndMigrating(StoreCase):
    def test_v3_produced_navigation_uses_the_migrated_legacy_marker(self):
        first = self.store.produce("nav-1", None, "next", "")
        second = self.store.produce("nav-2", None, "back", "")
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            row = db.execute(
                "SELECT body_json FROM session WHERE singleton = 1"
            ).fetchone()
            original = json.loads(row[0])
            original["number"] = 7
            db.execute(
                "UPDATE session SET body_json = ? WHERE singleton = 1",
                (json.dumps(original),),
            )
            db.execute("PRAGMA user_version = 3")

        upgraded = session_store.SessionStore(self.root)
        moved = dict(original, cursor=2, current_beat=2)
        result = {"kind": "walk", "cursor": 2, "current_beat": 2}
        self.assertEqual(
            upgraded.apply(first["seq"], result, session=moved)["state"],
            "applied",
        )
        upgraded.ack(first["seq"])
        current, beats = upgraded.snapshot()
        current.pop("legacy_pr")
        self.assertEqual(
            upgraded.reconcile_action(
                second["seq"],
                result,
                session=current,
                beats=beats,
                evidence="the migrated legacy session is already on disk",
            )["state"],
            "applied",
        )

    def test_legacy_marker_wins_over_changed_visible_identity_on_replay(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        (root / "beats").mkdir()
        (root / "session.json").write_text(
            json.dumps({**SESSION, "number": 7}), encoding="utf-8"
        )
        store = session_store.SessionStore(root)
        store.patch_session({"repo": "other/project", "number": 99})
        first = store.produce("nav-1", None, "next", "")
        moved = dict(store.snapshot()[0], cursor=2, current_beat=2)
        result = {"kind": "walk", "cursor": 2, "current_beat": 2}
        applied = store.apply(first["seq"], result, session=moved)

        self.assertEqual(store.apply(first["seq"], result, session=moved), applied)
        store.ack(first["seq"])
        second = store.produce("nav-2", None, "back", "")
        current, beats = store.snapshot()
        reconciled = store.reconcile_action(
            second["seq"],
            result,
            session=current,
            beats=beats,
            evidence="the diverged legacy identity is already on disk",
        )
        self.assertEqual(
            store.reconcile_action(
                second["seq"],
                result,
                session=current,
                beats=beats,
                evidence="the same receipt is replayed",
            ),
            reconciled,
        )

    def test_v3_navigation_receipt_keeps_its_own_historical_identity(self):
        action = self.store.produce("nav-1", None, "next", "")
        moved = dict(self.store.snapshot()[0], cursor=2, current_beat=2)
        result = {"kind": "walk", "cursor": 2, "current_beat": 2}
        applied = self.store.apply(action["seq"], result, session=moved)

        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            row = db.execute(
                "SELECT body_json FROM session WHERE singleton = 1"
            ).fetchone()
            current = json.loads(row[0])
            current["number"] = 7
            db.execute(
                "UPDATE session SET body_json = ? WHERE singleton = 1",
                (json.dumps(current),),
            )
            db.execute("PRAGMA user_version = 3")

        upgraded = session_store.SessionStore(self.root)

        self.assertEqual(
            upgraded.snapshot()[0]["legacy_pr"],
            {"repo": "acme/widget", "number": 7},
        )
        self.assertEqual(
            upgraded.apply(action["seq"], result, session=moved), applied
        )
        self.assertEqual(
            session_store.SessionStore(self.root).snapshot()[0],
            upgraded.snapshot()[0],
        )

    def test_v3_targetless_pr_navigation_replays_old_and_current_envelopes(self):
        action = self.store.produce("nav-1", None, "next", "")
        moved = dict(self.store.snapshot()[0], cursor=2, current_beat=2)
        result = {"kind": "walk", "cursor": 2, "current_beat": 2}
        applied = self.store.apply(action["seq"], result, session=moved)
        original_v3_session = dict(moved)
        original_v3_session.update({
            "number": 7,
            "legacy_pr": {"repo": "other/project", "number": 99},
            "execution_policy": {"mode": "sandboxed"},
        })

        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            row = db.execute(
                "SELECT body_json FROM session WHERE singleton = 1"
            ).fetchone()
            body = json.loads(row[0])
            body.update(original_v3_session)
            db.execute(
                "UPDATE session SET body_json = ? WHERE singleton = 1",
                (json.dumps(body),),
            )
            row = db.execute(
                "SELECT result_json FROM actions WHERE seq = ?", (action["seq"],)
            ).fetchone()
            application = json.loads(row[0])
            application["session"] = dict(original_v3_session)
            db.execute(
                "UPDATE actions SET result_json = ? WHERE seq = ?",
                (json.dumps(application), action["seq"]),
            )
            db.execute("PRAGMA user_version = 3")

        self.store = session_store.SessionStore(self.root)
        current, _beats = self.store.snapshot()

        self.assertEqual(
            current["legacy_pr"], {"repo": "acme/widget", "number": 7}
        )
        self.assertNotIn("execution_policy", current)
        self.assertEqual(
            self.store.apply(
                action["seq"], result, session=original_v3_session
            ),
            applied,
        )
        self.assertEqual(
            self.store.apply(action["seq"], result, session=current), applied
        )

    def test_legacy_import_discards_a_user_execution_policy(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        (root / "beats").mkdir()
        (root / "session.json").write_text(
            json.dumps({
                **SESSION,
                "number": 7,
                "execution_policy": {"mode": "sandboxed"},
            }),
            encoding="utf-8",
        )

        session = session_store.SessionStore(root).snapshot()[0]

        self.assertNotIn("execution_policy", session)
        self.assertEqual(
            session["legacy_pr"], {"repo": "acme/widget", "number": 7}
        )

    def test_legacy_import_preserves_a_valid_legacy_pr_marker_fail_closed(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        (root / "beats").mkdir()
        (root / "session.json").write_text(
            json.dumps({
                **SESSION,
                "legacy_pr": {"repo": "other/project", "number": 99},
            }),
            encoding="utf-8",
        )

        store = session_store.SessionStore(root)

        self.assertEqual(
            store.snapshot()[0]["legacy_pr"],
            {"repo": "other/project", "number": 99},
        )

    def test_export_reimport_preserves_legacy_pr_after_visible_identity_is_removed(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        (root / "beats").mkdir()
        (root / "session.json").write_text(
            json.dumps({
                **SESSION,
                "number": 7,
                "audience": {"mode": "branch", "why": "the author owns the branch"},
            }),
            encoding="utf-8",
        )
        (root / "beats" / "01.json").write_text(
            json.dumps(FLAG), encoding="utf-8"
        )
        store = session_store.SessionStore(root)
        marker = store.snapshot()[0]["legacy_pr"]
        store.patch_session({"repo": None, "number": None})
        store.export_json()
        (root / "session.sqlite3").unlink()

        restored = session_store.SessionStore(root)

        self.assertEqual(restored.snapshot()[0]["legacy_pr"], marker)
        with self.assertRaisesRegex(
            session_store.Conflict, "supervised replacement"
        ):
            restored.produce("accept-1", 1, "accept", "yes")

    def test_v3_upgrade_discards_a_user_legacy_pr_field(self):
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            row = db.execute(
                "SELECT body_json FROM session WHERE singleton = 1"
            ).fetchone()
            body = json.loads(row[0])
            body["legacy_pr"] = "user metadata"
            db.execute(
                "UPDATE session SET body_json = ? WHERE singleton = 1",
                (json.dumps(body),),
            )
            db.execute("PRAGMA user_version = 3")

        upgraded = session_store.SessionStore(self.root)

        self.assertNotIn("legacy_pr", upgraded.snapshot()[0])

    def test_v3_targetless_pr_discards_a_user_execution_policy(self):
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            row = db.execute(
                "SELECT body_json FROM session WHERE singleton = 1"
            ).fetchone()
            body = json.loads(row[0])
            body["number"] = 7
            body["execution_policy"] = {"mode": "sandboxed"}
            db.execute(
                "UPDATE session SET body_json = ? WHERE singleton = 1",
                (json.dumps(body),),
            )
            db.execute("PRAGMA user_version = 3")

        session = session_store.SessionStore(self.root).snapshot()[0]

        self.assertNotIn("execution_policy", session)
        self.assertEqual(
            session["legacy_pr"], {"repo": "acme/widget", "number": 7}
        )

    def test_v3_targetless_non_pr_discards_a_user_execution_policy(self):
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            row = db.execute(
                "SELECT body_json FROM session WHERE singleton = 1"
            ).fetchone()
            body = json.loads(row[0])
            body["execution_policy"] = {"mode": "user metadata"}
            db.execute(
                "UPDATE session SET body_json = ? WHERE singleton = 1",
                (json.dumps(body),),
            )
            db.execute("PRAGMA user_version = 3")

        first = session_store.SessionStore(self.root).snapshot()[0]
        second = session_store.SessionStore(self.root).snapshot()[0]

        self.assertNotIn("execution_policy", first)
        self.assertEqual(second, first)

    def test_a_pre_target_legacy_pr_requires_supervised_replacement(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        (root / "beats").mkdir()
        (root / "session.json").write_text(
            json.dumps({**SESSION, "number": 7}), encoding="utf-8"
        )
        (root / "beats" / "01.json").write_text(
            json.dumps(FLAG), encoding="utf-8"
        )
        store = session_store.SessionStore(root)

        with self.assertRaisesRegex(
            session_store.Conflict, "supervised replacement"
        ):
            store.produce("accept-1", 1, "accept", "yes")

        session, _beats = store.snapshot()
        without_marker = dict(session)
        without_marker.pop("legacy_pr")
        without_marker.pop("repo")
        without_marker.pop("number")
        with self.assertRaisesRegex(
            session_store.Conflict, "legacy PR identity cannot change"
        ):
            store.put_session(without_marker)

        store.patch_session({"repo": None, "number": None})
        with self.assertRaisesRegex(
            session_store.Conflict, "supervised replacement"
        ):
            store.produce("accept-2", 1, "accept", "yes")

        action = store.produce("nav-1", None, "next", "")
        moved = dict(store.snapshot()[0], cursor=2, current_beat=2)
        result = {"kind": "walk", "cursor": 2, "current_beat": 2}
        self.assertEqual(
            store.apply(action["seq"], result, session=moved)["state"],
            "applied",
        )
        store.ack(action["seq"])
        recovery = store.produce("nav-2", None, "back", "")
        current, beats = store.snapshot()
        self.assertEqual(
            store.reconcile_action(
                recovery["seq"],
                {"kind": "walk", "cursor": 2, "current_beat": 2},
                session=current,
                beats=beats,
                evidence="the legacy session is already on disk",
            )["state"],
            "applied",
        )

        self.assertEqual(store.snapshot()[1][0]["state"], "flag")

    def downgrade_to_pre_identity_v1(self, format_version=1):
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            db.execute("PRAGMA foreign_keys = OFF")
            db.execute("""CREATE TABLE legacy_session (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                format_version INTEGER NOT NULL,
                render_revision INTEGER NOT NULL DEFAULT 0 CHECK (render_revision >= 0),
                body_json TEXT NOT NULL
            )""")
            db.execute(
                "INSERT INTO legacy_session "
                "SELECT singleton, ?, render_revision, body_json FROM session",
                (format_version,),
            )
            db.execute("DROP TABLE session")
            db.execute("ALTER TABLE legacy_session RENAME TO session")
            db.execute("PRAGMA user_version = 1")

    def test_the_database_and_export_format_are_versioned(self):
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        db = self.store._connect()
        try:
            self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 3)
        finally:
            db.close()

        session, beats = self.store.snapshot()
        self.assertEqual(session["schema_version"], 1)
        self.assertEqual([beat["n"] for beat in beats], [1, 2])

    def test_v3_non_pr_sessions_do_not_gain_a_pr_execution_policy(self):
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            db.execute("PRAGMA user_version = 3")

        upgraded = session_store.SessionStore(self.root)

        self.assertNotIn("execution_policy", upgraded.snapshot()[0])
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 5)

    def test_the_session_identity_survives_restart_but_not_recreation(self):
        first = self.store.delivery_state()["session_id"]

        self.assertEqual(
            session_store.SessionStore(self.root).delivery_state()["session_id"],
            first,
        )
        (self.root / "session.sqlite3").unlink()

        recreated = session_store.SessionStore(self.root).delivery_state()["session_id"]
        self.assertNotEqual(recreated, first)

    def test_a_pre_identity_v1_database_upgrades_without_losing_state(self):
        action = self.store.produce("legacy-nav", None, "next", "")
        before = self.store.snapshot()
        self.downgrade_to_pre_identity_v1()

        upgraded = session_store.SessionStore(self.root)

        self.assertEqual(upgraded.snapshot(), before)
        self.assertEqual(upgraded.head()["action_id"], action["action_id"])
        self.assertTrue(upgraded.delivery_state()["session_id"])
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 5)
            columns = {row[1] for row in db.execute("PRAGMA table_info(session)")}
        self.assertIn("session_id", columns)

    def test_a_v2_upgrade_restores_the_fix_approved_before_delivery_failed(self):
        action = self.store.produce("click-1", 1, "accept", "yes")
        self.store.fail(action["seq"], "tests failed", "retry with the fixture")
        beat = self.beat(1)
        beat["slots"]["fix"] = "retry with the fixture"
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            db.execute(
                "UPDATE beats SET body_json = ? WHERE n = 1",
                (json.dumps(beat),),
            )
            db.execute("PRAGMA user_version = 2")

        upgraded = session_store.SessionStore(self.root)

        self.assertEqual(upgraded.snapshot()[1][0]["slots"]["fix"], "pin it")
        failed = upgraded.presentation_snapshot()[1][0]["delivery"]
        self.assertEqual(failed["owed"], "retry with the fixture")
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 5)

    def test_a_v2_upgrade_removes_a_failure_injected_fix(self):
        beat = self.beat(1)
        beat["slots"].pop("fix")
        self.store.put_beat(beat)
        action = self.store.produce("click-1", 1, "accept", "yes")
        self.store.fail(action["seq"], "tests failed", "retry with the fixture")
        beat = self.beat(1)
        beat["slots"]["fix"] = "retry with the fixture"
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            db.execute(
                "UPDATE beats SET body_json = ? WHERE n = 1",
                (json.dumps(beat),),
            )
            db.execute("PRAGMA user_version = 2")

        upgraded = session_store.SessionStore(self.root)

        self.assertNotIn("fix", upgraded.snapshot()[1][0]["slots"])
        failed = upgraded.presentation_snapshot()[1][0]["delivery"]
        self.assertEqual(failed["owed"], "retry with the fixture")

    def test_an_unsupported_document_is_not_partially_upgraded(self):
        self.downgrade_to_pre_identity_v1(format_version=99)

        with self.assertRaisesRegex(
            session_store.StoreError, "unsupported session format version 99"
        ):
            session_store.SessionStore(self.root)

        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            columns = {row[1] for row in db.execute("PRAGMA table_info(session)")}
        self.assertNotIn("session_id", columns)

    def test_a_failed_v1_upgrade_rolls_back_its_schema_change(self):
        self.downgrade_to_pre_identity_v1()

        with mock.patch.object(
            session_store.uuid, "uuid4", side_effect=OSError("identity unavailable")
        ):
            with self.assertRaisesRegex(OSError, "identity unavailable"):
                session_store.SessionStore(self.root)

        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            columns = {row[1] for row in db.execute("PRAGMA table_info(session)")}
        self.assertNotIn("session_id", columns)

    def test_concurrent_first_open_runs_one_migration(self):
        (self.root / "session.sqlite3").unlink()
        original = session_store.SessionStore._migrate_legacy
        calls, errors = [], []
        ready = threading.Barrier(2)

        def migrate(store, override):
            calls.append(store)
            time.sleep(0.05)
            return original(store, override)

        def start():
            ready.wait()
            try:
                session_store.SessionStore(self.root)
            except Exception as error:
                errors.append(error)

        with mock.patch.object(session_store.SessionStore, "_migrate_legacy", migrate):
            threads = [threading.Thread(target=start) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)

        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 1)

    def test_a_future_database_is_refused_without_changing_its_journal_mode(self):
        (self.root / "session.sqlite3").unlink()
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode = WAL").fetchone()[0], "wal")
            db.execute("PRAGMA user_version = 6")

        with self.assertRaisesRegex(session_store.StoreError, "newer than supported"):
            session_store.SessionStore(self.root)

        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_legacy_actions_without_an_ack_migrate_as_handled(self):
        (self.root / "session.sqlite3").unlink()
        (self.root / "decisions.jsonl").write_text(
            json.dumps({"seq": 1, "n": None, "action": "next", "note": ""}) + "\n",
            encoding="utf-8",
        )

        store = session_store.SessionStore(self.root)

        self.assertIsNone(store.head())
        self.assertEqual(store.ack(1), {"handled_seq": 1})

    def test_a_pending_legacy_accept_keeps_its_cause_sequence(self):
        (self.root / "session.sqlite3").unlink()
        accepted = dict(FLAG, state="accepted", call="yes")
        (self.root / "beats" / "01.json").write_text(
            json.dumps(accepted), encoding="utf-8"
        )
        (self.root / "decisions.jsonl").write_text(
            json.dumps({
                "seq": 1,
                "action_id": "legacy-accept",
                "n": 1,
                "action": "accept",
                "note": "yes",
                "delivery_version": 1,
            }) + "\n",
            encoding="utf-8",
        )
        (self.root / "ack.json").write_text(
            json.dumps({"version": 1, "handled_seq": 0}), encoding="utf-8"
        )

        store = session_store.SessionStore(self.root)

        self.assertEqual(
            store.reconcile()["pending_deliveries"],
            [{"beat_n": 1, "cause_seq": 1, "kind": "commit"}],
        )

    def test_a_landed_legacy_accept_needs_no_historical_action_log(self):
        (self.root / "session.sqlite3").unlink()
        accepted = dict(
            FLAG,
            state="accepted",
            landed="a" * 40,
            delivery_kind="commit",
            branch="feature",
        )
        (self.root / "beats" / "01.json").write_text(
            json.dumps(accepted), encoding="utf-8"
        )

        store = session_store.SessionStore(self.root)

        delivered = store.presentation_snapshot()[1][0]["delivery"]
        self.assertEqual(delivered["state"], "landed")
        self.assertEqual(delivered["artifact"], "a" * 40)

    def test_a_pending_legacy_pr_review_is_blocked_for_replacement(self):
        (self.root / "session.sqlite3").unlink()
        (self.root / "session.json").write_text(
            json.dumps({
                **SESSION,
                "number": 7,
                "audience": {"mode": "review", "why": "another reviewer owns the PR"},
            }),
            encoding="utf-8",
        )
        accepted = dict(FLAG, state="accepted", call="include it")
        (self.root / "beats" / "01.json").write_text(
            json.dumps(accepted), encoding="utf-8"
        )
        (self.root / "decisions.jsonl").write_text(
            json.dumps({
                "seq": 1,
                "action_id": "legacy-review",
                "n": 1,
                "action": "accept",
                "note": "include it",
                "delivery_version": 1,
            }) + "\n",
            encoding="utf-8",
        )
        (self.root / "ack.json").write_text(
            json.dumps({"version": 1, "handled_seq": 0}), encoding="utf-8"
        )

        pending = session_store.SessionStore(self.root).reconcile()[
            "pending_deliveries"
        ]

        self.assertEqual(pending[0]["kind"], "review")
        self.assertTrue(pending[0]["blocked"])
        self.assertIn("do not perform external delivery", pending[0]["blocked_reason"])

    def test_a_pending_legacy_accept_without_an_action_is_refused(self):
        (self.root / "session.sqlite3").unlink()
        (self.root / "beats" / "01.json").write_text(
            json.dumps(dict(FLAG, state="accepted")), encoding="utf-8"
        )

        with self.assertRaisesRegex(session_store.MigrationError, "no accept action"):
            session_store.SessionStore(self.root)

    def test_a_legacy_navigation_does_not_freeze_a_missing_audience(self):
        (self.root / "session.sqlite3").unlink()
        (self.root / "session.json").write_text(
            json.dumps({"repo": "acme/widget", "cursor": 1}), encoding="utf-8"
        )
        (self.root / "decisions.jsonl").write_text(
            json.dumps({"seq": 1, "n": None, "action": "next", "note": ""}) + "\n",
            encoding="utf-8",
        )

        store = session_store.SessionStore(self.root)
        updated = store.patch_session({
            "audience": {"mode": "branch", "why": "the author owns the branch"}
        })

        self.assertEqual(updated["audience"]["mode"], "branch")

    def test_cursor_aware_actions_with_no_ack_require_a_supervised_override(self):
        (self.root / "session.sqlite3").unlink()
        (self.root / "decisions.jsonl").write_text(
            json.dumps({
                "seq": 1,
                "n": None,
                "action": "next",
                "note": "",
                "delivery_version": 1,
            }) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(session_store.MigrationError, "--handled-seq"):
            session_store.SessionStore(self.root)

        overridden = session_store.SessionStore(self.root, handled_override=1)
        self.assertIsNone(overridden.head())

    def test_corrupt_ack_and_noncontiguous_actions_are_refused(self):
        (self.root / "session.sqlite3").unlink()
        records = (
            {"seq": 1, "n": None, "action": "next", "note": ""},
            {"seq": 3, "n": None, "action": "skip", "note": ""},
        )
        (self.root / "decisions.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        (self.root / "ack.json").write_text("{broken", encoding="utf-8")

        with self.assertRaisesRegex(session_store.MigrationError, "not contiguous"):
            session_store.SessionStore(self.root)

        (self.root / "decisions.jsonl").write_text(
            json.dumps(records[0]) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(session_store.MigrationError, "--handled-seq"):
            session_store.SessionStore(self.root)

    def test_a_malformed_action_line_is_not_silently_discarded(self):
        (self.root / "session.sqlite3").unlink()
        (self.root / "decisions.jsonl").write_text(
            '{"seq":1,"action":"next"}\n{"seq":2', encoding="utf-8"
        )

        with self.assertRaisesRegex(session_store.MigrationError, "line 2 is malformed"):
            session_store.SessionStore(self.root)

    def test_delivery_versions_require_integers_and_utf8(self):
        (self.root / "session.sqlite3").unlink()
        (self.root / "decisions.jsonl").write_text(
            json.dumps({
                "seq": 1, "n": None, "action": "next", "note": "",
                "delivery_version": 1.0,
            }) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(session_store.MigrationError, "unsupported"):
            session_store.SessionStore(self.root)

        (self.root / "decisions.jsonl").write_bytes(b"\xff")
        with self.assertRaisesRegex(session_store.MigrationError, "valid UTF-8"):
            session_store.SessionStore(self.root)

    def test_a_failed_import_never_publishes_the_temporary_database(self):
        (self.root / "session.sqlite3").unlink()
        (self.root / "session.json").write_text("{broken", encoding="utf-8")

        with self.assertRaises(json.JSONDecodeError):
            session_store.SessionStore(self.root)

        self.assertFalse((self.root / "session.sqlite3").exists())

    def test_a_failed_publish_fsync_is_retried_on_the_next_open(self):
        (self.root / "session.sqlite3").unlink()
        with mock.patch.object(
            session_store, "_fsync_directory", side_effect=OSError("disk")
        ):
            with self.assertRaises(OSError):
                session_store.SessionStore(self.root)

        self.assertTrue((self.root / "session.sqlite3").exists())
        with mock.patch.object(
            session_store,
            "_fsync_directory",
            wraps=session_store._fsync_directory,
        ) as synced:
            session_store.SessionStore(self.root)
        synced.assert_called_with(self.root)

    def test_a_handled_override_cannot_silently_change_an_existing_database(self):
        with self.assertRaisesRegex(session_store.StoreError, "only valid"):
            session_store.SessionStore(self.root, handled_override=0)


class ProducingAndApplying(StoreCase):
    def test_a_deterministic_action_mutates_the_beat_and_records_one_applied_action(self):
        action = self.store.produce("click-1", 1, "accept", "yes")

        self.assertEqual((action["seq"], action["state"]), (1, "applied"))
        self.assertEqual(self.beat(1)["state"], "accepted")
        self.assertEqual(self.beat(1)["call"], "yes")
        self.assertEqual(self.store.reconcile()["pending_deliveries"][0]["beat_n"], 1)

    def test_action_id_deduplicates_an_http_retry(self):
        first = self.store.produce("click-1", 1, "accept", "yes")
        again = self.store.produce("click-1", 1, "accept", "yes")

        self.assertEqual(again, first)
        self.assertEqual(self.store.head()["seq"], 1)
        with self.assertRaises(session_store.Conflict):
            self.store.produce("click-1", 1, "drop", "no")

    def test_a_fault_between_beat_and_action_rolls_the_whole_transaction_back(self):
        before = self.beat(1)
        with mock.patch.object(self.store, "_insert_action", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.store.produce("click-1", 1, "accept", "yes")

        self.assertEqual(self.beat(1), before)
        self.assertIsNone(self.store.head())

    def test_navigation_apply_is_absolute_and_idempotent(self):
        action = self.store.produce("nav-1", None, "next", "")
        session = dict(self.store.snapshot()[0], cursor=2, current_beat=2)
        result = {"kind": "walk", "cursor": 2, "current_beat": 2, "tier": "core"}

        applied = self.store.apply(action["seq"], result, session=session, beats=(CLEAN,))
        revision = self.store.reconcile()["render_revision"]
        replay = self.store.apply(action["seq"], result, session=session, beats=(CLEAN,))

        self.assertEqual(applied, replay)
        self.assertEqual(self.store.snapshot()[0]["current_beat"], 2)
        self.assertEqual(self.store.reconcile()["render_revision"], revision)
        with self.assertRaises(session_store.Conflict):
            self.store.apply(action["seq"], dict(result, current_beat=3), session=session)

        with self.assertRaisesRegex(session_store.Conflict, "different application"):
            self.store.apply(
                action["seq"], result, session=dict(session, title="different")
            )

    def test_a_navigation_fault_rolls_back_the_absolute_state_and_receipt(self):
        action = self.store.produce("nav-1", None, "next", "")
        session = dict(self.store.snapshot()[0], cursor=2, current_beat=2)

        with mock.patch.object(self.store, "_bump_render", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.store.apply(
                    action["seq"], {"kind": "walk", "cursor": 2}, session=session
                )

        self.assertNotIn("current_beat", self.store.snapshot()[0])
        self.assertEqual(self.store.head()["state"], "produced")

    def test_a_beat_action_cannot_overtake_unapplied_navigation(self):
        navigation = self.store.produce("nav-1", None, "next", "")

        with self.assertRaisesRegex(session_store.Conflict, "must be applied"):
            self.store.produce("click-1", 1, "accept", "yes")

        self.assertEqual(self.beat(1)["state"], "flag")
        self.assertEqual(self.store.delivery_state()["seq"], navigation["seq"])

    def test_process_death_inside_navigation_rolls_back_on_restart(self):
        action = self.store.produce("nav-1", None, "next", "")
        code = "\n".join((
            "import importlib.util, os",
            f"spec = importlib.util.spec_from_file_location('store', {str(SCRIPTS / 'session_store.py')!r})",
            "module = importlib.util.module_from_spec(spec)",
            "spec.loader.exec_module(module)",
            f"store = module.SessionStore({str(self.root)!r})",
            "session = dict(store.snapshot()[0], cursor=2, current_beat=2)",
            "store._bump_render = lambda db: os._exit(91)",
            f"store.apply({action['seq']}, {{'kind':'walk','cursor':2}}, session=session)",
        ))

        died = subprocess.run([sys.executable, "-c", code])

        self.assertEqual(died.returncode, 91)
        resumed = session_store.SessionStore(self.root)
        self.assertNotIn("current_beat", resumed.snapshot()[0])
        self.assertEqual(resumed.head()["state"], "produced")

    def test_ack_requires_the_applied_head_and_preserves_order(self):
        first = self.store.produce("nav-1", None, "next", "")
        second = self.store.produce("nav-2", None, "skip", "")

        with self.assertRaises(session_store.Conflict):
            self.store.ack(first["seq"])
        with self.assertRaises(session_store.Conflict):
            self.store.apply(second["seq"], {"kind": "walk", "cursor": 3})

        session = dict(self.store.snapshot()[0], cursor=2, current_beat=2)
        self.store.apply(first["seq"], {"kind": "walk", "cursor": 2}, session=session)
        self.assertEqual(self.store.ack(first["seq"]), {"handled_seq": 1})
        self.assertEqual(self.store.head()["seq"], 2)

    def test_a_legacy_accept_can_be_refined_into_a_decision(self):
        self.assertNotIn("resolution_kind", self.beat(1))
        accepted = self.store.produce("click-1", 1, "accept", "yes")
        decided = self.store.produce("decision-1", 1, "decide", "stays as is")

        self.assertEqual(self.beat(1)["state"], "decided")
        self.assertNotIn("resolution_kind", self.beat(1))
        self.assertEqual(self.store.ack(accepted["seq"]), {"handled_seq": 1})
        self.assertEqual(self.store.ack(decided["seq"]), {"handled_seq": 2})

    def test_new_flags_default_to_delivery_and_reject_decide(self):
        stored = self.store.put_beat(dict(FLAG, n=3))

        self.assertEqual(stored["resolution_kind"], "delivery")
        with self.assertRaisesRegex(session_store.Conflict, "resolution_kind 'delivery'"):
            self.store.produce("decision-1", 3, "decide", "leave it")

        accepted = self.store.produce("click-1", 3, "accept", "implement it")
        self.assertEqual(accepted["result"]["state"], "accepted")

    def test_clean_beats_do_not_carry_unused_resolution_metadata(self):
        stored = self.store.put_beat(dict(CLEAN, n=3))

        self.assertNotIn("resolution_kind", stored)

    def test_explicit_decision_beats_reject_accept_and_require_words(self):
        self.store.put_beat(dict(FLAG, n=3, resolution_kind="decision"))

        with self.assertRaisesRegex(session_store.Conflict, "resolution_kind 'decision'"):
            self.store.produce("click-1", 3, "accept", "yes")
        with self.assertRaisesRegex(session_store.StoreError, "non-empty note"):
            self.store.produce("decision-1", 3, "decide", "")

        decided = self.store.produce("decision-2", 3, "decide", "leave it")
        self.assertEqual(decided["result"]["state"], "decided")
        self.assertEqual(decided["result"]["delivery"], "none")

    def test_invalid_resolution_kinds_are_rejected(self):
        for resolution_kind in (None, "patch", 1):
            with self.subTest(resolution_kind=resolution_kind):
                with self.assertRaisesRegex(session_store.StoreError, "resolution_kind"):
                    self.store.put_beat(
                        dict(FLAG, n=3, resolution_kind=resolution_kind)
                    )

    def test_navigation_defaults_a_new_beat_to_delivery_idempotently(self):
        new = dict(FLAG, n=3)
        action = self.store.produce("nav-1", None, "next", "")
        session = dict(self.store.snapshot()[0], current_beat=3)
        result = {"kind": "walk", "current_beat": 3}

        applied = self.store.apply(
            action["seq"], result, session=session, beats=(new,)
        )
        replay = self.store.apply(
            action["seq"], result, session=session, beats=(new,)
        )

        self.assertEqual(replay, applied)
        self.assertEqual(self.beat(3)["resolution_kind"], "delivery")

    def test_put_beat_preserves_store_owned_reviewer_fields(self):
        stale = self.beat(1)
        self.store.produce("click-1", 1, "accept", "reviewer words")
        stale["slots"]["proof"] = "`python -m unittest`"

        updated = self.store.put_beat(stale)

        self.assertEqual(updated["state"], "accepted")
        self.assertEqual(updated["call"], "reviewer words")
        self.assertEqual(updated["slots"]["proof"], "`python -m unittest`")

    def test_put_beat_cannot_change_a_resolved_resolution_kind(self):
        stored = self.store.put_beat(
            dict(FLAG, n=3, resolution_kind="delivery")
        )
        self.store.produce("click-1", 3, "accept", "implement it")
        stored["resolution_kind"] = "decision"

        updated = self.store.put_beat(stored)

        self.assertEqual(updated["resolution_kind"], "delivery")

    def test_put_beat_preserves_an_omitted_open_resolution_kind(self):
        self.store.put_beat(dict(FLAG, n=3, resolution_kind="delivery"))
        stale = dict(FLAG, n=3, claim="clearer claim")

        updated = self.store.put_beat(stale)

        self.assertEqual(updated["resolution_kind"], "delivery")
        with self.assertRaisesRegex(session_store.Conflict, "resolution_kind 'delivery'"):
            self.store.produce("decision-1", 3, "decide", "leave it")

    def test_put_beat_can_explicitly_change_an_open_resolution_kind(self):
        stored = self.store.put_beat(dict(FLAG, n=3))
        stored["resolution_kind"] = "decision"

        updated = self.store.put_beat(stored)

        self.assertEqual(updated["resolution_kind"], "decision")
        with self.assertRaisesRegex(session_store.Conflict, "resolution_kind 'decision'"):
            self.store.produce("click-1", 3, "accept", "implement it")

    def test_an_older_empty_decide_still_deduplicates_by_action_id(self):
        self.store.produce("decision-1", 1, "decide", "stays as is")
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            db.execute(
                "UPDATE actions SET note = '' WHERE action_id = 'decision-1'"
            )

        replay = self.store.produce("decision-1", 1, "decide", "")

        self.assertEqual(replay["action_id"], "decision-1")
        self.assertEqual(replay["note"], "")

    def test_new_beats_must_open_unresolved(self):
        accepted = dict(FLAG, n=3, state="accepted")
        with self.assertRaisesRegex(session_store.StoreError, "new beat state"):
            self.store.put_beat(accepted)

        action = self.store.produce("nav-1", None, "next", "")
        session = dict(self.store.snapshot()[0], current_beat=3)
        with self.assertRaisesRegex(session_store.StoreError, "new beat state"):
            self.store.apply(
                action["seq"],
                {"kind": "walk", "current_beat": 3},
                session=session,
                beats=(accepted,),
            )
        self.assertEqual(self.store.head()["state"], "produced")

    def test_new_beats_cannot_preload_store_owned_delivery_fields(self):
        prelanded = dict(
            FLAG,
            n=3,
            landed="abc1234",
            branch="jacek/fix",
            delivery_kind="commit",
            delivery={"state": "landed"},
        )

        with self.assertRaisesRegex(session_store.StoreError, "store-owned"):
            self.store.put_beat(prelanded)

    def test_navigation_cannot_replace_a_beat_changed_after_it_was_queued(self):
        stale = self.beat(1)
        action = self.store.produce("nav-1", None, "next", "")
        self.store.put_beat(dict(stale, claim="newer claim"))
        session = dict(self.store.snapshot()[0], current_beat=2)

        with self.assertRaisesRegex(session_store.Conflict, "cannot replace existing"):
            self.store.apply(
                action["seq"], {"kind": "walk", "current_beat": 2},
                session=session, beats=(stale,),
            )

        self.assertEqual(self.beat(1)["claim"], "newer claim")
        self.assertEqual(self.store.head()["state"], "produced")

    def test_invalid_audience_never_bypasses_branch_delivery(self):
        for audience in ({"mode": None}, {"mode": "typo"}, "review"):
            with self.subTest(audience=audience):
                with self.assertRaises(session_store.StoreError):
                    self.store.put_session(
                        dict(self.store.snapshot()[0], audience=audience)
                    )

    def test_audience_cannot_change_after_an_accept(self):
        action = self.store.produce("click-1", 1, "accept", "yes")

        with self.assertRaisesRegex(session_store.StoreError, "frozen PR target"):
            self.store.patch_session({"audience": {"mode": "review", "why": "changed"}})

        self.assertEqual(self.store.snapshot()[0]["audience"]["mode"], "branch")
        with self.assertRaisesRegex(session_store.Conflict, "has not landed"):
            self.store.ack(action["seq"])

    def test_separate_connections_deduplicate_and_observe_the_same_action(self):
        stores = (self.store, session_store.SessionStore(self.root))
        ready = threading.Barrier(2)
        results = []

        def submit(store):
            ready.wait()
            results.append(store.produce("nav-1", None, "next", ""))

        threads = [threading.Thread(target=submit, args=(store,)) for store in stores]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)

        self.assertEqual([result["seq"] for result in results], [1, 1])
        self.assertEqual(stores[1].delivery_state()["seq"], 1)

    def test_separate_connections_resolve_one_flag_only_once(self):
        stores = (self.store, session_store.SessionStore(self.root))
        ready = threading.Barrier(2)
        results = []

        def resolve(store, action):
            ready.wait()
            try:
                results.append(store.produce(action, 1, action, ""))
            except session_store.Conflict as error:
                results.append(error)

        threads = [
            threading.Thread(target=resolve, args=(store, action))
            for store, action in zip(stores, ("accept", "drop"))
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)

        committed = [result for result in results if isinstance(result, dict)]
        self.assertEqual(len(committed), 1)
        self.assertEqual(self.beat(1)["state"], committed[0]["result"]["state"])
        self.assertEqual(self.store.delivery_state()["seq"], 1)

    def test_reconcile_records_only_an_absolute_result_already_on_disk(self):
        action = self.store.produce("nav-1", None, "back", "")
        current_session, current_beats = self.store.snapshot()
        result = {"kind": "walk", "cursor": current_session["cursor"], "current_beat": 1}

        receipt = self.store.reconcile_action(
            action["seq"], result, session=current_session, beats=current_beats,
            evidence="session files already show beat 1",
        )

        self.assertEqual(receipt["state"], "applied")
        with self.assertRaises(session_store.Conflict):
            self.store.reconcile_action(
                action["seq"],
                dict(result, current_beat=2),
                evidence="the stored state was inspected",
            )


class RecoveringAndDelivering(StoreCase):
    def test_supervised_abandon_only_moves_the_exact_head(self):
        first = self.store.produce("nav-1", None, "next", "")
        second = self.store.produce("nav-2", None, "skip", "")

        with self.assertRaises(session_store.Conflict):
            self.store.abandon_head(second["seq"], "jacek", "wrong action")
        abandoned = self.store.abandon_head(first["seq"], "jacek", "legacy move is ambiguous")

        self.assertEqual(abandoned["state"], "abandoned")
        self.assertEqual(self.store.head()["seq"], second["seq"])
        self.assertEqual(self.store.reconcile()["handled_seq"], first["seq"])

    def test_an_applied_action_cannot_be_abandoned(self):
        action = self.store.produce("nav-1", None, "next", "")
        session = dict(self.store.snapshot()[0], current_beat=2)
        self.store.apply(
            action["seq"], {"kind": "walk", "current_beat": 2}, session=session
        )

        with self.assertRaisesRegex(session_store.Conflict, "cannot be abandoned"):
            self.store.abandon_head(action["seq"], "jacek", "changed my mind")

    def test_fail_is_durable_and_land_can_complete_the_same_accept(self):
        action = self.store.produce("click-1", 1, "accept", "yes")
        failed = self.store.fail(action["seq"], "tests failed", "fix the fixture")

        self.assertEqual(failed["state"], "failed")
        self.assertEqual(self.store.fail(action["seq"], "tests failed", "fix the fixture"), failed)
        self.assertEqual(failed["owed"], "fix the fixture")
        self.assertEqual(self.beat(1)["slots"]["fix"], "pin it")
        self.assertEqual(self.store.head()["seq"], action["seq"])
        with self.assertRaisesRegex(session_store.Conflict, "has not landed"):
            self.store.ack(action["seq"])

        landed = self.store.land(
            action["seq"], 1, "abc1234", "commit", branch="jacek/fix",
            land_entry={"state": "landed", "what": "pin it", "where": "abc1234"},
        )
        self.assertEqual(landed["state"], "landed")
        replay = self.store.land(
            action["seq"], 1, "abc1234", "commit", branch="jacek/fix"
        )
        self.assertEqual(replay["artifact"], "abc1234")
        self.assertEqual(self.beat(1)["landed"], "abc1234")
        self.assertEqual(len(self.store.snapshot()[0]["lands"]), 1)
        with self.assertRaisesRegex(session_store.Conflict, "already landed"):
            self.store.land(
                action["seq"],
                1,
                "abc1234",
                "commit",
                branch="jacek/fix",
                land_entry={"state": "landed", "what": "different", "where": "abc1234"},
            )

    def test_a_later_failure_replaces_the_nonterminal_failure(self):
        action = self.store.produce("click-1", 1, "accept", "yes")
        self.store.fail(action["seq"], "lint failed", "fix lint")

        latest = self.store.fail(action["seq"], "tests failed", "fix tests")

        self.assertEqual(latest["error"], "tests failed")
        self.assertEqual(latest["owed"], "fix tests")
        self.assertEqual(self.beat(1)["slots"]["fix"], "pin it")
        self.assertEqual(
            self.store.reconcile()["failed_deliveries"][0]["error"],
            "tests failed",
        )

    def test_delivery_kind_matches_the_frozen_audience(self):
        action = self.store.produce("click-1", 1, "accept", "yes")

        with self.assertRaisesRegex(session_store.Conflict, "does not match"):
            self.store.land(action["seq"], 1, "review-url", "review")
        with self.assertRaisesRegex(session_store.StoreError, "requires a branch"):
            self.store.land(action["seq"], 1, "abc1234", "commit")

        self.store.land(
            action["seq"], 1, "abc1234", "commit", branch="jacek/fix"
        )
        with self.assertRaisesRegex(session_store.Conflict, "already landed"):
            self.store.produce("decision-1", 1, "decide", "undo it")

    def test_delivery_cannot_overtake_the_action_head(self):
        third = dict(FLAG, n=3, claim="another flag")
        self.store.put_beat(third)
        first = self.store.produce("click-1", 1, "accept", "yes")
        second = self.store.produce("click-2", 3, "accept", "yes")

        with self.assertRaisesRegex(session_store.Conflict, "at the head"):
            self.store.land(
                second["seq"], 3, "def5678", "commit", branch="jacek/second"
            )
        self.store.fail(first["seq"], "tests failed", "fix tests")

    def test_a_land_fault_rolls_back_both_the_beat_and_session_entry(self):
        action = self.store.produce("click-1", 1, "accept", "yes")

        with mock.patch.object(self.store, "_save_session", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.store.land(
                    action["seq"], 1, "abc1234", "commit", branch="jacek/fix"
                )

        session, beats = self.store.snapshot()
        self.assertEqual(session["lands"], [])
        self.assertNotIn("landed", next(beat for beat in beats if beat["n"] == 1))
        self.assertEqual(self.store.reconcile()["pending_deliveries"][0]["beat_n"], 1)

    def test_a_session_patch_cannot_erase_or_forge_a_landed_entry(self):
        action = self.store.produce("click-1", 1, "accept", "yes")
        self.store.land(
            action["seq"], 1, "abc1234", "commit", branch="jacek/fix"
        )

        self.store.patch_session({"title": "new title"})
        with self.assertRaisesRegex(session_store.Conflict, "through land"):
            self.store.patch_session({
                "lands": [{"state": "landed", "what": "fake", "where": "bad"}]
            })
        with self.assertRaisesRegex(session_store.Conflict, "before actions"):
            self.store.put_session(dict(self.store.snapshot()[0], title="replacement"))

        session = self.store.snapshot()[0]
        self.assertEqual(session["title"], "new title")
        self.assertEqual(session["lands"][0]["where"], "abc1234")

    def test_a_stale_beat_write_cannot_replace_the_approved_fix(self):
        stale = self.beat(1)
        action = self.store.produce("click-1", 1, "accept", "yes")
        self.store.fail(action["seq"], "tests failed", "fix the fixture")
        stale["claim"] = "clearer claim"
        stale["slots"]["fix"] = "different implementation intent"

        updated = self.store.put_beat(stale)

        self.assertEqual(updated["claim"], "clearer claim")
        self.assertEqual(updated["slots"]["fix"], "pin it")
        presented = self.store.presentation_snapshot()[1][0]
        self.assertEqual(presented["delivery"]["owed"], "fix the fixture")

    def test_presentation_snapshot_projects_delivery_without_persisting_it(self):
        raw_session, raw_beats = self.store.snapshot()
        presented_session, presented_beats = self.store.presentation_snapshot()

        self.assertEqual(presented_session, raw_session)
        self.assertNotIn("delivery", raw_beats[0])
        self.assertEqual(presented_beats[0]["delivery"], {"state": "none"})

        action = self.store.produce("click-1", 1, "accept", "yes")
        pending = self.store.presentation_snapshot()[1][0]["delivery"]
        self.assertEqual(
            pending,
            {"state": "pending", "cause_seq": action["seq"], "kind": "commit"},
        )

        self.store.fail(action["seq"], "tests failed", "fix the fixture")
        failed = self.store.presentation_snapshot()[1][0]["delivery"]
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["error"], "tests failed")
        self.assertEqual(failed["owed"], "fix the fixture")
        self.assertNotIn("delivery", self.beat(1))

    def test_export_regenerates_the_legacy_projection(self):
        first = self.store.produce("click-1", 1, "accept", "yes")
        self.store.land(
            first["seq"], 1, "abc1234", "commit", branch="jacek/fix"
        )
        self.store.ack(first["seq"])
        self.store.export_json()

        session = json.loads((self.root / "session.json").read_text(encoding="utf-8"))
        beat = json.loads((self.root / "beats" / "01.json").read_text(encoding="utf-8"))
        decisions = [
            json.loads(line)
            for line in (self.root / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        ack = json.loads((self.root / "ack.json").read_text(encoding="utf-8"))

        self.assertEqual(session["schema_version"], 1)
        self.assertEqual(beat["state"], "accepted")
        self.assertEqual(decisions[0]["action"], "accept")
        self.assertEqual(decisions[0]["action_id"], "click-1")
        self.assertEqual(ack, {"version": 1, "handled_seq": 1})

    def test_an_export_failure_never_rolls_back_authoritative_state(self):
        action = self.store.produce("click-1", 1, "accept", "yes")
        self.store.land(
            action["seq"], 1, "abc1234", "commit", branch="jacek/fix"
        )

        with mock.patch.object(session_store, "_atomic_write", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.store.export_json()

        resumed = session_store.SessionStore(self.root)
        beat = next(beat for beat in resumed.snapshot()[1] if beat["n"] == 1)
        self.assertEqual(beat["landed"], "abc1234")
        resumed.export_json()
        self.assertEqual(
            json.loads((self.root / "beats" / "01.json").read_text(encoding="utf-8"))["landed"],
            "abc1234",
        )

    def test_an_older_export_cannot_overwrite_a_newer_one(self):
        self.store.put_session(dict(self.store.snapshot()[0], title="old"))
        entered, release = threading.Event(), threading.Event()
        original = session_store._atomic_write
        blocked = []

        def delayed(path, payload):
            if threading.current_thread().name == "old-export" and not blocked:
                blocked.append(True)
                entered.set()
                release.wait(5)
            return original(path, payload)

        old = threading.Thread(target=self.store.export_json, name="old-export")

        def update_and_export():
            newer = session_store.SessionStore(self.root)
            newer.put_session(dict(newer.snapshot()[0], title="new"))
            newer.export_json()

        new = threading.Thread(target=update_and_export, name="new-export")
        with mock.patch.object(session_store, "_atomic_write", delayed):
            old.start()
            self.assertTrue(entered.wait(5))
            new.start()
            time.sleep(0.05)
            self.assertTrue(new.is_alive())
            release.set()
            old.join(5)
            new.join(5)

        exported = json.loads(
            (self.root / "session.json").read_text(encoding="utf-8")
        )
        self.assertEqual(exported["title"], "new")


class LinkedImplementations(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.source_root = self.root / "source"
        self.source = session_store.SessionStore(self.source_root)
        self.target = {
            "version": 1,
            "kind": "github_pr",
            "repo": "acme/widget",
            "number": 17,
            "state": "open",
            "merged_at": None,
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "head_repo_id": 123,
            "head_repo": "acme/widget",
            "head_ref": "feature",
            "merge_base_sha": "c" * 40,
            "changed_files": 1,
        }
        diff = self.root / "pr.diff"
        metadata = self.root / "pr.json"
        context = self.root / "trusted-context.json"
        bundle = self.root / "pr.bundle"
        diff.write_bytes(b"diff --git a/a.py b/a.py\n")
        metadata.write_text('{"number":17}\n', encoding="utf-8")
        context.write_text(
            json.dumps(
                {"version": 1, "base_sha": "a" * 40, "files": []},
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        bundle.write_bytes(b"frozen object bundle")
        self.target = self.source.freeze_target(
            self.target, diff, metadata, context, bundle
        )
        self.source.put_beat(FLAG)
        action = self.source.produce("accept-source", 1, "accept", "approved finding")
        self.source.ack(action["seq"])
        self.source_action = action["seq"]

    def profile(self):
        return {
            "version": 1,
            "keyId": "sha256:" + "d" * 64,
            "signerId": "urn:underwrite:signer:test",
            "executorId": "urn:underwrite:executor:test",
            "job": {"argv": ["python3", "fix.py"]},
            "sandbox": {"limits": {"workspaceBytes": 1024, "outputBytes": 1024}},
            "exitCode": 0,
        }

    def authorize(self, actor="reviewer", approval="implement this finding"):
        return self.source.authorize_implementation(
            self.source_action, 1, actor, approval
        )

    def create_child(self):
        link = self.authorize()
        created = self.source.create_linked_implementation(link["link_id"])
        ready = self.source.complete_implementation_link(
            link["link_id"], created["child_session_id"]
        )
        self.assertEqual(ready["state"], "ready")
        return session_store.SessionStore(created["child_root"]), link, created

    def evidence(self, attempt, capability=b"capability", receipt=b"receipt"):
        profile = attempt["trusted_profile"]
        return capability, receipt, {
            "version": 1,
            "requestSha256": attempt["request_sha256"],
            "keyId": profile["keyId"],
            "signerId": profile["signerId"],
            "executorId": profile["executorId"],
            "capabilitySha256": hashlib.sha256(capability).hexdigest(),
            "receiptSha256": hashlib.sha256(receipt).hexdigest(),
            "inputTree": "e" * 64,
            "outputTree": "f" * 64,
            "outputBundle": {"sha256": "1" * 64, "bytes": 19},
            "stdout": {"sha256": hashlib.sha256(b"").hexdigest(), "bytes": 0, "truncated": False},
            "stderr": {"sha256": hashlib.sha256(b"").hexdigest(), "bytes": 0, "truncated": False},
            "exitCode": profile["exitCode"],
        }

    def test_authorization_is_distinct_idempotent_and_does_not_mutate_the_source(self):
        with sqlite3.connect(str(self.source_root / "session.sqlite3")) as db:
            before = db.execute(
                "SELECT body_json, render_revision FROM session WHERE singleton = 1"
            ).fetchone()
            beat_before = db.execute("SELECT * FROM beats WHERE n = 1").fetchone()

        link = self.authorize()
        self.assertEqual(self.authorize(), link)
        self.assertEqual(link["source_beat"], 1)
        self.assertEqual(link["child_path"], f"implementations/{link['link_id']}")
        self.assertEqual(
            link["branch"], f"underwrite/implementation-{link['link_id'][:16]}"
        )
        with self.assertRaisesRegex(session_store.Conflict, "different implementation"):
            self.authorize(approval="different approval")
        with self.assertRaisesRegex(session_store.Conflict, "not the accept"):
            self.source.authorize_implementation(self.source_action, 2, "reviewer", "yes")

        with sqlite3.connect(str(self.source_root / "session.sqlite3")) as db:
            self.assertEqual(
                db.execute(
                    "SELECT body_json, render_revision FROM session WHERE singleton = 1"
                ).fetchone(),
                before,
            )
            self.assertEqual(db.execute("SELECT * FROM beats WHERE n = 1").fetchone(), beat_before)

    def test_create_is_recoverable_and_seeds_one_immutable_gateway_child(self):
        link = self.authorize()
        with sqlite3.connect(str(self.source_root / "session.sqlite3")) as db:
            source_before = db.execute(
                "SELECT body_json, render_revision FROM session WHERE singleton = 1"
            ).fetchone()
            beat_before = db.execute("SELECT * FROM beats WHERE n = 1").fetchone()
        created = self.source.create_linked_implementation(link["link_id"])
        replay = self.source.create_linked_implementation(link["link_id"])
        self.assertEqual(replay, created)
        self.source.complete_implementation_link(
            link["link_id"], created["child_session_id"]
        )
        with sqlite3.connect(str(self.source_root / "session.sqlite3")) as db:
            self.assertEqual(
                db.execute(
                    "SELECT body_json, render_revision FROM session WHERE singleton = 1"
                ).fetchone(),
                source_before,
            )
            self.assertEqual(
                db.execute("SELECT * FROM beats WHERE n = 1").fetchone(), beat_before
            )
        child = session_store.SessionStore(created["child_root"])
        session, beats = child.snapshot()
        self.assertEqual(session["audience"]["mode"], "branch")
        self.assertEqual(session["delivery_branch"], link["branch"])
        self.assertEqual(session["execution_policy"]["mode"], "gateway_attested")
        self.assertEqual(session["execution_policy"]["link_id"], link["link_id"])
        self.assertEqual(session["linked_implementation"]["target_sha256"], link["target_sha256"])
        self.assertEqual([beat["n"] for beat in beats], [1])
        self.assertEqual(beats[0]["state"], "accepted")
        self.assertEqual(child.head()["action_id"], f"implementation:{link['link_id']}")
        pending = child.reconcile()["pending_deliveries"]
        self.assertEqual(pending[0]["kind"], "commit")
        self.assertEqual(child.verify_target_files(), self.source.verify_target_files())

        with self.assertRaisesRegex(session_store.Conflict, "verified output application"):
            child.check_execution()
        with self.assertRaisesRegex(session_store.Conflict, "reserved for the attested gateway"):
            child.produce("ordinary-action", 1, "note", "bypass")
        with self.assertRaisesRegex(session_store.Conflict, "implementation gateway"):
            child.land(1, 1, "9" * 40, "commit", branch=link["branch"])
        with self.assertRaisesRegex(session_store.Conflict, "finding is immutable"):
            child.put_beat(beats[0])
        with self.assertRaisesRegex(session_store.Conflict, "must use the implementation gateway"):
            child.fail(1, "bypass", "retry")
        changed = dict(session["linked_implementation"], actor="attacker")
        with self.assertRaisesRegex(session_store.Conflict, "metadata is immutable"):
            child.patch_session({"linked_implementation": changed})

    def test_precreated_symlink_cannot_redirect_the_child_session(self):
        link = self.authorize()
        implementation_root = self.source_root / "implementations"
        implementation_root.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (implementation_root / link["link_id"]).symlink_to(
            outside, target_is_directory=True
        )

        with self.assertRaisesRegex(session_store.Conflict, "real directory"):
            self.source.create_linked_implementation(link["link_id"])
        self.assertEqual(list(outside.iterdir()), [])

    def test_foreign_child_session_is_rejected_before_projection_replacement(self):
        link = self.authorize()
        child_root = self.source_root / link["child_path"]
        foreign = session_store.SessionStore(child_root)
        foreign.patch_session({"title": "foreign session"})
        marker = child_root / "pr.diff"
        marker.write_bytes(b"foreign projection\n")
        before = {
            path.name: path.read_bytes()
            for path in child_root.iterdir()
            if path.is_file()
        }

        with self.assertRaisesRegex(session_store.Conflict, "projection pr.diff changed"):
            self.source.create_linked_implementation(link["link_id"])

        after = {
            path.name: path.read_bytes()
            for path in child_root.iterdir()
            if path.is_file()
        }
        self.assertEqual(after, before)

    def test_foreign_directory_without_database_is_never_adopted(self):
        link = self.authorize()
        child_root = self.source_root / link["child_path"]
        child_root.mkdir(parents=True)
        (child_root / "session.json").write_bytes(b'{"title":"foreign"}\n')
        (child_root / "notes.txt").write_bytes(b"keep me\n")
        before = {
            path.name: path.read_bytes()
            for path in child_root.iterdir()
        }

        with self.assertRaisesRegex(session_store.Conflict, "database cannot be inspected"):
            self.source.create_linked_implementation(link["link_id"])

        self.assertEqual(
            {path.name: path.read_bytes() for path in child_root.iterdir()},
            before,
        )

    def test_hard_linked_foreign_database_is_never_opened_as_a_child(self):
        link = self.authorize()
        foreign_root = self.root / "foreign"
        foreign = session_store.SessionStore(foreign_root)
        foreign.patch_session({"title": "foreign session"})
        database = foreign_root / "session.sqlite3"
        before = database.read_bytes()
        child_root = self.source_root / link["child_path"]
        child_root.mkdir(parents=True)
        os.link(database, child_root / "session.sqlite3")
        (child_root / ".session.lock").write_bytes(b"")

        with self.assertRaisesRegex(session_store.Conflict, "database"):
            self.source.create_linked_implementation(link["link_id"])

        self.assertEqual(database.read_bytes(), before)
        self.assertEqual(foreign.snapshot()[0]["title"], "foreign session")
        self.assertFalse((child_root / "pr.diff").exists())

    def test_child_database_replacement_blocks_link_completion(self):
        link = self.authorize()
        created = self.source.create_linked_implementation(link["link_id"])
        child_root = Path(created["child_root"])
        database = child_root / "session.sqlite3"
        external = self.root / "external-child.sqlite3"
        database.rename(external)
        database.symlink_to(external)
        before = external.read_bytes()

        with self.assertRaisesRegex(session_store.Conflict, "database must be"):
            self.source.complete_implementation_link(
                link["link_id"], created["child_session_id"]
            )

        self.assertEqual(external.read_bytes(), before)

    def test_crash_before_child_publish_leaves_no_partial_final_directory(self):
        link = self.authorize()
        with mock.patch.object(
            session_store.SessionStore,
            "_initialize_linked_child",
            side_effect=RuntimeError("stopped before child publish"),
        ):
            with self.assertRaisesRegex(RuntimeError, "stopped"):
                self.source.create_linked_implementation(link["link_id"])

        child_root = self.source_root / link["child_path"]
        self.assertFalse(child_root.exists())
        self.assertEqual(list(child_root.parent.iterdir()), [])
        created = self.source.create_linked_implementation(link["link_id"])
        ready = self.source.complete_implementation_link(
            link["link_id"], created["child_session_id"]
        )

        self.assertEqual(ready["state"], "ready")
        child = session_store.SessionStore(child_root)
        self.assertEqual(
            child.snapshot()[0]["linked_implementation"]["link_id"],
            link["link_id"],
        )

    def test_published_child_with_a_hot_journal_is_not_opened_or_recovered(self):
        link = self.authorize()
        self.source.create_linked_implementation(link["link_id"])
        child_root = self.source_root / link["child_path"]
        database = child_root / "session.sqlite3"
        crashed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os,sqlite3,sys; "
                "db=sqlite3.connect(sys.argv[1]); "
                "db.execute('PRAGMA journal_mode=DELETE'); "
                "db.execute('BEGIN IMMEDIATE'); "
                "db.execute(\"UPDATE session SET body_json='{\\\"partial\\\":true}'\"); "
                "os._exit(0)",
                str(database),
            ],
            check=False,
        )
        self.assertEqual(crashed.returncode, 0)
        self.assertTrue((child_root / "session.sqlite3-journal").exists())
        before = {
            path.name: path.read_bytes()
            for path in child_root.iterdir()
            if path.is_file()
        }

        with self.assertRaisesRegex(session_store.Conflict, "unfinished journal"):
            self.source.create_linked_implementation(link["link_id"])

        self.assertEqual(
            {
                path.name: path.read_bytes()
                for path in child_root.iterdir()
                if path.is_file()
            },
            before,
        )

    def test_changed_source_beat_cannot_seed_an_authorized_child(self):
        link = self.authorize()
        with sqlite3.connect(str(self.source_root / "session.sqlite3")) as db:
            row = db.execute("SELECT body_json FROM beats WHERE n = 1").fetchone()
            beat = json.loads(row[0])
            beat["slots"]["what"] = "changed after authorization"
            db.execute(
                "UPDATE beats SET revision = revision + 1, body_json = ? WHERE n = 1",
                (json.dumps(beat, separators=(",", ":"), sort_keys=True),),
            )

        with self.assertRaisesRegex(session_store.Conflict, "revision moved"):
            self.source.create_linked_implementation(link["link_id"])
        self.assertFalse((self.source_root / link["child_path"] / "session.sqlite3").exists())

    def test_attempt_reservation_and_failure_replay_are_exact(self):
        child, _link, _created = self.create_child()
        first = child.reserve_implementation_attempt(1, self.profile())
        self.assertEqual(first["state"], "reserved")
        self.assertEqual(child.reserve_implementation_attempt(1, self.profile()), first)
        self.assertEqual(len(first["challenge"]), 64)
        self.assertEqual(first["request"]["challenge"], first["challenge"])
        self.assertEqual(first["request"]["action"], {"seq": 1, "beat": 1, "attempt": 1})
        changed = dict(self.profile(), executorId="different")
        with self.assertRaisesRegex(session_store.Conflict, "different trusted profile"):
            child.reserve_implementation_attempt(1, changed)

        failed = child.fail_implementation_attempt(1, 1, "gateway unavailable")
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(
            child.fail_implementation_attempt(1, 1, "gateway unavailable"), failed
        )
        with self.assertRaisesRegex(session_store.Conflict, "different reason"):
            child.fail_implementation_attempt(1, 1, "another failure")
        resumed = session_store.SessionStore(child.root)
        second = resumed.reserve_implementation_attempt(1, self.profile())
        self.assertEqual(second["attempt"], 2)
        self.assertNotEqual(second["challenge"], first["challenge"])
        self.assertEqual(resumed.reconcile()["pending_deliveries"][0]["attempt"], 2)

    def test_attempt_profile_bounds_fail_before_reservation(self):
        child, _link, _created = self.create_child()
        profiles = []
        oversized = self.profile()
        oversized["job"]["argv"].append(
            "x" * session_store.MAX_IMPLEMENTATION_PROFILE_BYTES
        )
        profiles.append(("byte limit", oversized))
        exit_code = self.profile()
        exit_code["exitCode"] = 256
        profiles.append(("exitCode", exit_code))

        for name, profile in profiles:
            with self.subTest(name=name):
                with self.assertRaises(session_store.StoreError):
                    child.reserve_implementation_attempt(1, profile)
                with sqlite3.connect(str(child.root / "session.sqlite3")) as db:
                    self.assertEqual(
                        db.execute(
                            "SELECT COUNT(*) FROM implementation_attempts"
                        ).fetchone()[0],
                        0,
                    )

    def test_verified_plan_survives_restart_and_only_exact_commit_lands(self):
        child, link, _created = self.create_child()
        attempt = child.reserve_implementation_attempt(1, self.profile())
        capability, receipt, evidence = self.evidence(attempt)
        verified = child.record_verified_implementation(
            1, 1, capability, receipt, evidence
        )
        self.assertEqual(verified["state"], "verified")
        self.assertEqual(
            child.record_verified_implementation(1, 1, capability, receipt, evidence),
            verified,
        )
        plan = {
            "version": 1,
            "commit": "2" * 40,
            "parent": self.target["head_sha"],
            "tree": "3" * 40,
            "outputTree": evidence["outputTree"],
            "branch": link["branch"],
        }
        prepared = child.prepare_implementation_land(1, 1, plan)
        self.assertEqual(prepared["state"], "prepared")
        restarted = session_store.SessionStore(child.root)
        self.assertEqual(restarted.implementation_attempt(1, 1)["commit_plan"], plan)
        with self.assertRaisesRegex(session_store.Conflict, "persisted plan"):
            restarted.finish_implementation_land(1, 1, "4" * 40, link["branch"])
        landed = restarted.finish_implementation_land(
            1, 1, plan["commit"], plan["branch"]
        )
        self.assertEqual(landed["artifact"], plan["commit"])
        self.assertEqual(
            restarted.finish_implementation_land(1, 1, plan["commit"], plan["branch"]),
            landed,
        )
        self.assertEqual(restarted.implementation_attempt(1, 1)["state"], "landed")
        self.assertFalse(restarted.delivery_state()["recovery"])

    def test_plan_rejects_a_moved_parent_and_unverified_output(self):
        child, link, _created = self.create_child()
        attempt = child.reserve_implementation_attempt(1, self.profile())
        capability, receipt, evidence = self.evidence(attempt)
        child.record_verified_implementation(1, 1, capability, receipt, evidence)
        base = {
            "version": 1,
            "commit": "2" * 40,
            "parent": "4" * 40,
            "tree": "3" * 40,
            "outputTree": evidence["outputTree"],
            "branch": link["branch"],
        }
        with self.assertRaisesRegex(session_store.Conflict, "frozen target head"):
            child.prepare_implementation_land(1, 1, base)
        base["parent"] = self.target["head_sha"]
        base["outputTree"] = "5" * 64
        with self.assertRaisesRegex(session_store.Conflict, "verified output"):
            child.prepare_implementation_land(1, 1, base)

    def test_v4_upgrade_adds_authority_tables_without_changing_session_state(self):
        before = self.source.snapshot()
        with sqlite3.connect(str(self.source_root / "session.sqlite3")) as db:
            db.execute("DROP TABLE implementation_attempts")
            db.execute("DROP TABLE implementation_links")
            db.execute("PRAGMA user_version = 4")

        upgraded = session_store.SessionStore(self.source_root)

        self.assertEqual(upgraded.snapshot(), before)
        with sqlite3.connect(str(self.source_root / "session.sqlite3")) as db:
            names = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 5)
        self.assertIn("implementation_links", names)
        self.assertIn("implementation_attempts", names)

    def test_linked_json_export_cannot_be_reimported_without_attempt_authority(self):
        child, _link, _created = self.create_child()
        child.reserve_implementation_attempt(1, self.profile())
        child.export_json()
        (child.root / "session.sqlite3").unlink()

        with self.assertRaisesRegex(session_store.MigrationError, "authoritative database"):
            session_store.SessionStore(child.root)


if __name__ == "__main__":
    unittest.main()
