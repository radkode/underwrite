#!/usr/bin/env python3
"""Command-line contracts for the transactional session store."""

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "underwrite" / "scripts"
SESSIONCTL = SCRIPTS / "sessionctl.py"
SPEC = importlib.util.spec_from_file_location("session_store", SCRIPTS / "session_store.py")
session_store = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(session_store)

SESSION = {
    "repo": "acme/widget",
    "number": 7,
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


class SessionCtlCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "beats").mkdir()
        (self.root / "session.json").write_text(json.dumps(SESSION), encoding="utf-8")
        (self.root / "beats" / "01.json").write_text(json.dumps(FLAG), encoding="utf-8")

    def invoke(self, *args, value=None, stdin=None):
        if value is not None:
            stdin = json.dumps(value)
        return subprocess.run(
            [sys.executable, str(SESSIONCTL), *map(str, args)],
            input=stdin,
            text=True,
            capture_output=True,
        )

    def success(self, *args, value=None):
        completed = self.invoke(*args, value=value)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def init(self):
        return self.success("init", self.root)

    def store(self):
        return session_store.SessionStore(self.root)


class InitializingAndDocuments(SessionCtlCase):
    def test_init_can_supervise_a_legacy_cursor_and_exports_it(self):
        (self.root / "decisions.jsonl").write_text(
            json.dumps({
                "seq": 1,
                "action_id": "legacy-nav",
                "n": None,
                "action": "next",
                "note": "",
                "delivery_version": 1,
            })
            + "\n",
            encoding="utf-8",
        )

        result = self.success("init", self.root, "--handled-seq", 1)

        self.assertIsNone(result["head"])
        self.assertTrue((self.root / "session.sqlite3").exists())
        ack = json.loads((self.root / "ack.json").read_text(encoding="utf-8"))
        self.assertEqual(ack["handled_seq"], 1)

    def test_put_session_and_beat_accept_stdin_or_a_file_and_export(self):
        self.init()
        session = dict(SESSION, cursor=2, current_beat=1)
        result = self.success("put-session", self.root, value=session)
        self.assertEqual(result["cursor"], 2)

        beat = dict(FLAG, claim="inspect the release")
        source = self.root / "beat-input.json"
        source.write_text(json.dumps(beat), encoding="utf-8")
        result = self.success("put-beat", self.root, source)

        self.assertEqual(result["claim"], "inspect the release")
        self.assertNotIn("call", result)
        exported = json.loads((self.root / "beats" / "01.json").read_text(encoding="utf-8"))
        self.assertEqual(exported, beat)
        exported_session = json.loads((self.root / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(exported_session["cursor"], 2)

        (self.root / "session.json").unlink()
        projection = self.success("export", self.root)
        self.assertEqual(projection["beats"], 1)
        self.assertTrue((self.root / "session.json").exists())

    def test_get_and_patch_never_read_the_legacy_projection(self):
        self.init()
        patched = self.success(
            "patch-session", self.root, value={"title": "authoritative"}
        )
        self.assertEqual(patched["title"], "authoritative")
        (self.root / "session.json").write_text("{ stale", encoding="utf-8")
        (self.root / "beats" / "01.json").write_text("{ stale", encoding="utf-8")

        session = self.success("get-session", self.root)
        beat = self.success("get-beat", self.root, 1)

        self.assertEqual(session["title"], "authoritative")
        self.assertEqual(beat["claim"], "unpinned")


class ApplyingAndRecovering(SessionCtlCase):
    def test_apply_and_ack_publish_the_committed_absolute_result(self):
        self.init()
        store = self.store()
        action = store.produce("nav-1", None, "next", "")
        session = dict(store.snapshot()[0], cursor=2, current_beat=1)
        envelope = {
            "result": {"kind": "walk", "cursor": 2, "current_beat": 1},
            "session": session,
            "beats": [],
        }

        applied = self.success("apply", self.root, action["seq"], value=envelope)
        acknowledged = self.success("ack", self.root, action["seq"])

        self.assertEqual(applied["state"], "applied")
        self.assertEqual(acknowledged, {"handled_seq": 1})
        exported = json.loads((self.root / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(exported["cursor"], 2)
        ack = json.loads((self.root / "ack.json").read_text(encoding="utf-8"))
        self.assertEqual(ack["handled_seq"], 1)

    def test_fail_and_land_export_the_delivery_saga(self):
        self.init()
        action = self.store().produce("accept-1", 1, "accept", "yes")

        failed = self.success(
            "fail", self.root, action["seq"], "tests failed", "pin the release"
        )
        entry = self.root / "land-entry.json"
        entry.write_text(
            json.dumps({"state": "landed", "what": "pin it", "where": "abc1234"}),
            encoding="utf-8",
        )
        landed = self.success(
            "land",
            self.root,
            action["seq"],
            1,
            "abc1234",
            "--kind",
            "commit",
            "--branch",
            "jacek/pin",
            "--entry",
            entry,
        )

        self.assertEqual(failed["state"], "failed")
        self.assertEqual(landed["state"], "landed")
        beat = json.loads((self.root / "beats" / "01.json").read_text(encoding="utf-8"))
        self.assertEqual(beat["slots"]["fix"], "pin the release")
        self.assertEqual(beat["landed"], "abc1234")
        session = json.loads((self.root / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(session["lands"][0]["where"], "abc1234")

    def test_reconcile_action_requires_evidence_and_abandon_requires_the_head(self):
        self.init()
        store = self.store()
        first = store.produce("nav-1", None, "back", "")
        second = store.produce("nav-2", None, "skip", "")
        session, beats = store.snapshot()
        envelope = {
            "result": {"kind": "walk", "cursor": session["cursor"]},
            "session": session,
            "beats": beats,
            "evidence": "the authoritative documents already show this position",
        }

        reconciled = self.success(
            "reconcile-action", self.root, first["seq"], value=envelope
        )
        self.success("ack", self.root, first["seq"])
        abandoned = self.success(
            "abandon-head",
            self.root,
            second["seq"],
            "--actor",
            "jacek",
            "--reason",
            "the pre-kernel navigation was ambiguous",
        )

        self.assertEqual(reconciled["state"], "applied")
        self.assertEqual(abandoned["state"], "abandoned")
        state = self.success("reconcile", self.root)
        self.assertEqual(state["handled_seq"], second["seq"])
        self.assertIsNone(state["head"])


class Failures(SessionCtlCase):
    def test_usage_and_bad_input_exit_one_without_a_traceback(self):
        missing = self.invoke()
        malformed = self.invoke("put-session", self.root, stdin="{broken")

        self.assertEqual(missing.returncode, 1)
        self.assertIn("usage:", missing.stderr)
        self.assertEqual(malformed.returncode, 1)
        self.assertIn("sessionctl:", malformed.stderr)
        self.assertNotIn("Traceback", malformed.stderr)

    def test_reconcile_action_rejects_missing_evidence_without_mutating_the_head(self):
        self.init()
        action = self.store().produce("nav-1", None, "next", "")
        session, _beats = self.store().snapshot()
        envelope = {
            "result": {"kind": "walk", "cursor": session["cursor"]},
            "session": session,
        }

        failed = self.invoke(
            "reconcile-action", self.root, action["seq"], value=envelope
        )

        self.assertEqual(failed.returncode, 1)
        self.assertIn("requires evidence", failed.stderr)
        self.assertEqual(self.store().head()["state"], "produced")

    def test_a_store_conflict_exits_one_and_preserves_queue_order(self):
        self.init()
        store = self.store()
        first = store.produce("nav-1", None, "next", "")
        second = store.produce("nav-2", None, "skip", "")

        failed = self.invoke(
            "abandon-head",
            self.root,
            second["seq"],
            "--actor",
            "jacek",
            "--reason",
            "wrong head",
        )

        self.assertEqual(failed.returncode, 1)
        self.assertIn("at the head", failed.stderr)
        self.assertEqual(self.store().head()["seq"], first["seq"])


if __name__ == "__main__":
    unittest.main()
