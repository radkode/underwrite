#!/usr/bin/env python3
"""Focused contracts for the transactional underwrite session store."""
import importlib.util
import json
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


class CreatingAndMigrating(StoreCase):
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
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        db = self.store._connect()
        try:
            self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 3)
        finally:
            db.close()

        session, beats = self.store.snapshot()
        self.assertEqual(session["schema_version"], 1)
        self.assertEqual([beat["n"] for beat in beats], [1, 2])

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
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            columns = {row[1] for row in db.execute("PRAGMA table_info(session)")}
        self.assertIn("session_id", columns)

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
            db.execute("PRAGMA user_version = 3")

        with self.assertRaisesRegex(session_store.StoreError, "newer than supported"):
            session_store.SessionStore(self.root)

        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
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

    def test_review_accept_can_ack_before_the_single_review_lands(self):
        self.store.put_session(dict(self.store.snapshot()[0], audience={"mode": "review"}))
        action = self.store.produce("click-1", 1, "accept", "yes")

        self.assertEqual(self.store.ack(action["seq"]), {"handled_seq": 1})
        self.assertTrue(self.store.delivery_state()["recovery"])

    def test_a_later_decision_supersedes_an_accept_that_needs_no_commit(self):
        accepted = self.store.produce("click-1", 1, "accept", "yes")
        decided = self.store.produce("decision-1", 1, "decide", "stays as is")

        self.assertEqual(self.beat(1)["state"], "decided")
        self.assertEqual(self.store.ack(accepted["seq"]), {"handled_seq": 1})
        self.assertEqual(self.store.ack(decided["seq"]), {"handled_seq": 2})

    def test_put_beat_preserves_store_owned_reviewer_fields(self):
        stale = self.beat(1)
        self.store.produce("click-1", 1, "accept", "reviewer words")
        stale["slots"]["proof"] = "`python -m unittest`"

        updated = self.store.put_beat(stale)

        self.assertEqual(updated["state"], "accepted")
        self.assertEqual(updated["call"], "reviewer words")
        self.assertEqual(updated["slots"]["proof"], "`python -m unittest`")

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

        with self.assertRaisesRegex(session_store.Conflict, "audience cannot change"):
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
        self.assertEqual(self.beat(1)["slots"]["fix"], "fix the fixture")
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
        self.assertEqual(self.beat(1)["slots"]["fix"], "fix tests")
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

    def test_a_stale_beat_write_cannot_erase_a_failed_fix(self):
        stale = self.beat(1)
        action = self.store.produce("click-1", 1, "accept", "yes")
        self.store.fail(action["seq"], "tests failed", "fix the fixture")
        stale["claim"] = "clearer claim"

        updated = self.store.put_beat(stale)

        self.assertEqual(updated["claim"], "clearer claim")
        self.assertEqual(updated["slots"]["fix"], "fix the fixture")

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


if __name__ == "__main__":
    unittest.main()
