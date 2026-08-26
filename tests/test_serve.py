#!/usr/bin/env python3
"""Tests for the server, which is the half of the tool that has state and threads.

    python3 -m unittest discover -s tests

Covers what a file on disk cannot show: that two clients cannot resolve one flag
two ways, that a beat is never left half-written, that a park wakes when someone
acts and not before, that a malformed request gets an answer instead of dropping
the connection, and that everything the page needs reaches it without asking.
"""
import contextlib
import importlib.util
import io
import json
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "underwrite" / "scripts"


def load(stem):
    """Import a script whose filename is not a legal module name."""
    spec = importlib.util.spec_from_file_location(
        stem.replace("-", "_"), SCRIPTS / (stem + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


serve = load("serve")

FLAG = {
    "n": 1, "tier": "core", "state": "flag", "claim": "unpinned", "where": "a.py:1",
    "slots": {"what": "x", "proof": "a.py:1", "risk": "r", "fix": "pin it"},
}
CLEAN = {
    "n": 2, "tier": "core", "state": "clean", "claim": "load-bearing", "where": "b.py:1",
    "slots": {"what": "x", "proof": "b.py:1"},
}


class SessionTest(unittest.TestCase):
    """A session directory per test: beat 1 an open flag, beat 2 already clean."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "beats").mkdir()
        (self.root / "session.json").write_text(
            json.dumps({"repo": "acme/widget", "number": 1, "cursor": 2}), encoding="utf-8"
        )
        for beat in (FLAG, CLEAN):
            self.put(beat)
        self.session = self.open_session()

    def open_session(self):
        session = serve.Session(self.root, serve.rr().default_css())
        self.addCleanup(session.stop.set)
        return session

    def put(self, beat):
        self.path(beat["n"]).write_text(json.dumps(beat), encoding="utf-8")

    def path(self, n):
        return self.root / "beats" / ("%02d.json" % n)

    def beat(self, n):
        return json.loads(self.path(n).read_text(encoding="utf-8"))

    def decisions(self):
        log = self.root / "decisions.jsonl"
        if not log.exists():
            return []
        return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]


class ResolvingAFlag(SessionTest):
    def test_accept_resolves_it_and_records_the_words(self):
        record = self.session.act(1, "accept", "yes, pin it")
        self.assertEqual(record["seq"], 1)
        self.assertEqual(self.beat(1)["state"], "accepted")
        self.assertEqual(self.beat(1)["call"], "yes, pin it")

    def test_a_flag_resolves_only_once(self):
        self.session.act(1, "accept", "")
        with self.assertRaises(ValueError):
            self.session.act(1, "drop", "")
        self.assertEqual(self.beat(1)["state"], "accepted")
        self.assertEqual(self.session.seq, 1)
        self.assertEqual(len(self.decisions()), 1)

    def test_a_clean_beat_cannot_be_accepted(self):
        with self.assertRaises(ValueError):
            self.session.act(2, "accept", "")
        self.assertEqual(self.beat(2)["state"], "clean")

    def test_a_beat_that_does_not_exist_is_rejected(self):
        with self.assertRaises(ValueError):
            self.session.act(9, "accept", "")

    def test_a_note_records_words_without_resolving_anything(self):
        self.session.act(2, "note", "worth watching")
        self.assertEqual(self.beat(2)["state"], "clean")
        self.assertEqual(self.beat(2)["call"], "worth watching")

    def test_a_walk_action_touches_no_beat(self):
        before = self.path(1).read_bytes()
        self.session.act(None, "next", "")
        self.assertEqual(self.path(1).read_bytes(), before)
        self.assertEqual(self.decisions()[0]["action"], "next")


class Deciding(SessionTest):
    """The yes that resolves in words rather than a commit. Without it the beat sat
    accepted with nothing landed, which Phase 4 refuses to render clean, and the reviewer
    was told to fix a beat file that had no legal fix."""

    def test_it_resolves_an_open_flag(self):
        self.session.act(1, "decide", "stays as is")
        self.assertEqual(self.beat(1)["state"], "decided")
        self.assertEqual(self.beat(1)["call"], "stays as is")

    def test_it_also_takes_a_beat_the_reviewer_already_accepted(self):
        """The click said yes. Deciding records that the yes was a call rather than a
        patch, which is the same answer refined and not a second bite at it."""
        self.session.act(1, "accept", "yes")
        self.session.act(1, "decide", "")
        self.assertEqual(self.beat(1)["state"], "decided")
        self.assertEqual(self.beat(1)["call"], "yes")

    def test_it_cannot_reopen_what_the_reviewer_dropped(self):
        self.session.act(1, "drop", "no")
        with self.assertRaises(ValueError):
            self.session.act(1, "decide", "actually yes")
        self.assertEqual(self.beat(1)["state"], "dropped")

    def test_a_decided_beat_cannot_then_be_accepted(self):
        self.session.act(1, "decide", "stays as is")
        with self.assertRaises(ValueError):
            self.session.act(1, "accept", "")
        self.assertEqual(self.beat(1)["state"], "decided")

    def test_a_clean_beat_was_never_a_flag_to_decide(self):
        with self.assertRaises(ValueError):
            self.session.act(2, "decide", "x")
        self.assertEqual(self.beat(2)["state"], "clean")


class Concurrency(SessionTest):
    def test_two_clients_cannot_resolve_one_flag_two_ways(self):
        """Read, guard and write used to sit outside the lock, so both passed the
        guard: 101 of 200 rounds had accept and drop each return 200."""
        results, ready = {}, threading.Barrier(2)

        def go(action, i):
            ready.wait()
            try:
                results[i] = self.session.act(1, action, "")
            except ValueError as err:
                results[i] = err

        threads = [threading.Thread(target=go, args=a) for a in (("accept", 0), ("drop", 1))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5.0)

        self.assertEqual(len([r for r in results.values() if isinstance(r, dict)]), 1)
        self.assertEqual(len([r for r in results.values() if isinstance(r, ValueError)]), 1)
        self.assertEqual(len(self.decisions()), 1)
        self.assertIn(self.beat(1)["state"], ("accepted", "dropped"))

    def test_concurrent_actions_get_consecutive_seqs(self):
        threads = [threading.Thread(target=self.session.act, args=(None, "next", ""))
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5.0)
        self.assertEqual(sorted(r["seq"] for r in self.decisions()), list(range(1, 9)))


class AtomicWrites(SessionTest):
    def test_a_failed_rename_leaves_the_beat_byte_identical(self):
        before = self.path(1).read_bytes()
        with mock.patch.object(serve.os, "replace", side_effect=OSError("no space")):
            with self.assertRaises(OSError):
                self.session.act(1, "accept", "")
        self.assertEqual(self.path(1).read_bytes(), before)

    def test_a_failed_rename_leaves_no_temp_file_behind(self):
        with mock.patch.object(serve.os, "replace", side_effect=OSError("no space")):
            with self.assertRaises(OSError):
                self.session.act(1, "accept", "")
        self.assertEqual(sorted(p.name for p in (self.root / "beats").iterdir()),
                         ["01.json", "02.json"])

    def test_a_normal_write_leaves_no_temp_file_behind(self):
        self.session.act(1, "accept", "")
        self.assertEqual(sorted(p.name for p in (self.root / "beats").iterdir()),
                         ["01.json", "02.json"])

    def test_a_temp_file_is_never_globbed_as_a_beat(self):
        """The fingerprint and the renderer's loader both glob *.json, and pathlib
        globs dotfiles too, so the temp name must not end in .json."""
        (self.root / "beats" / "01.json.9.9.tmp").write_text("{ half written")
        self.assertEqual(sorted(p.name for p in (self.root / "beats").glob("*.json")),
                         ["01.json", "02.json"])


class Parking(SessionTest):
    def test_it_returns_the_oldest_unseen_action(self):
        for _ in range(3):
            self.session.act(None, "next", "")
        self.assertEqual(self.session.wait(0.1)["seq"], 1)
        self.session.ack(1)
        self.assertEqual(self.session.wait(0.1)["seq"], 2)

    def test_it_waits_the_whole_timeout_when_nobody_acts(self):
        started = time.monotonic()
        self.assertIsNone(self.session.wait(0.2))
        self.assertGreaterEqual(time.monotonic() - started, 0.2)

    def test_an_idle_park_does_not_reread_the_action_log(self):
        with mock.patch.object(self.session, "records", wraps=self.session.records) as records:
            with mock.patch.object(serve, "WAIT_SLICE", 0.001):
                self.assertIsNone(self.session.wait(0.01))

        records.assert_not_called()

    def test_a_blank_line_in_the_log_does_not_fake_a_timeout(self):
        """seq came from a line count while tail skipped blanks, so a single blank
        line put seq past the highest record and every park answered instantly.
        The agent is told that means nobody acted, so the walk span."""
        (self.root / "decisions.jsonl").write_text(
            json.dumps({"seq": 1, "n": None, "action": "next", "note": ""}) + "\n\n",
            encoding="utf-8",
        )
        session = self.open_session()
        self.assertEqual(session.seq, 1)
        session.ack(1)
        started = time.monotonic()
        self.assertIsNone(session.wait(0.2))
        self.assertGreaterEqual(time.monotonic() - started, 0.2)

    def test_nobody_parked_means_nobody_is_listening(self):
        self.assertFalse(self.session.snapshot()["listening"])

    def test_a_parked_caller_is_visible_to_the_page(self):
        """The page used to infer this from the agent's own status text, which an agent
        that died mid-walk leaves standing, so a click went nowhere and looked broken."""
        seen = []
        waiter = threading.Thread(target=lambda: self.session.wait(5.0))
        waiter.start()
        for _ in range(100):
            if self.session.snapshot()["listening"]:
                seen.append(True)
                break
            time.sleep(0.01)
        self.session.act(None, "next", "")
        waiter.join(5.0)
        self.assertEqual(seen, [True])
        self.assertFalse(self.session.snapshot()["listening"])

    def test_a_park_that_times_out_stops_listening(self):
        self.session.wait(0.05)
        self.assertFalse(self.session.snapshot()["listening"])

    def test_an_already_satisfied_park_never_claims_to_listen(self):
        """seq is already ahead, so wait returns without parking at all."""
        self.session.act(None, "next", "")
        self.assertIsNotNone(self.session.wait(5.0))
        self.assertFalse(self.session.snapshot()["listening"])

    def test_it_wakes_as_soon_as_someone_acts(self):
        woke = []
        waiter = threading.Thread(target=lambda: woke.append(self.session.wait(5.0)))
        waiter.start()
        time.sleep(0.05)
        self.session.act(None, "next", "")
        waiter.join(5.0)
        self.assertEqual(woke[0]["action"], "next")


class Acknowledging(SessionTest):
    def test_a_new_session_persists_zero_before_accepting_actions(self):
        stored = json.loads((self.root / "ack.json").read_text(encoding="utf-8"))
        self.assertEqual(stored, {"version": 1, "handled_seq": 0})

    def test_an_unhandled_action_is_replayed_after_restart(self):
        self.session.act(None, "next", "")

        resumed = self.open_session()

        self.assertEqual(resumed.handled_seq, 0)
        self.assertEqual(resumed.wait(0.1)["seq"], 1)

    def test_an_acknowledgement_survives_restart(self):
        self.session.act(None, "next", "")
        self.assertEqual(self.session.ack(1), {"handled_seq": 1})

        resumed = self.open_session()

        self.assertEqual(resumed.handled_seq, 1)
        self.assertIsNone(resumed.wait(0.05))

    def test_an_acknowledgement_does_not_change_rendered_content(self):
        self.session.act(None, "next", "")
        revision = self.session.fingerprint()

        self.session.ack(1)

        self.assertEqual(self.session.fingerprint(), revision)

    def test_acknowledgements_cannot_skip_an_unhandled_action(self):
        self.session.act(None, "next", "")
        self.session.act(None, "skip", "")

        with self.assertRaises(ValueError):
            self.session.ack(2)

        self.assertEqual(self.session.ack(1), {"handled_seq": 1})
        self.assertEqual(self.session.ack(2), {"handled_seq": 2})

    def test_retrying_the_latest_acknowledgement_is_idempotent(self):
        self.session.act(None, "next", "")
        self.session.ack(1)

        self.assertEqual(self.session.ack(1), {"handled_seq": 1})

    def test_a_late_retry_cannot_regress_the_cursor(self):
        self.session.act(None, "next", "")
        self.session.act(None, "skip", "")
        self.session.ack(1)
        self.session.ack(2)

        self.assertEqual(self.session.ack(1), {"handled_seq": 2})

    def test_an_acknowledgement_cannot_name_an_unproduced_action(self):
        with self.assertRaises(ValueError):
            self.session.ack(1)

    def test_a_failed_ack_write_does_not_advance_the_cursor(self):
        self.session.act(None, "next", "")

        with mock.patch.object(serve.os, "replace", side_effect=OSError("no space")):
            with self.assertRaises(OSError):
                self.session.ack(1)

        self.assertEqual(self.session.handled_seq, 0)

    def test_legacy_actions_are_migrated_as_already_handled(self):
        (self.root / "ack.json").unlink()
        (self.root / "decisions.jsonl").write_text(
            json.dumps({"seq": 1, "n": None, "action": "next", "note": ""}) + "\n",
            encoding="utf-8",
        )

        resumed = self.open_session()

        self.assertEqual(resumed.handled_seq, 1)
        self.assertIsNone(resumed.wait(0.05))

    def test_a_missing_ack_is_rejected_after_cursor_aware_actions(self):
        self.session.act(None, "next", "")
        (self.root / "ack.json").unlink()

        with self.assertRaisesRegex(ValueError, "missing from a cursor-aware session"):
            self.open_session()

    def test_an_unknown_action_delivery_version_is_rejected(self):
        (self.root / "decisions.jsonl").write_text(
            json.dumps({
                "seq": 1, "n": None, "action": "next", "note": "",
                "delivery_version": 2,
            }) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "unsupported action delivery version: 2"):
            self.open_session()

    def test_an_ack_file_that_is_not_an_object_is_rejected(self):
        (self.root / "ack.json").write_text("[]", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "must be a JSON object"):
            self.open_session()

    def test_an_ack_file_with_the_wrong_version_is_rejected(self):
        (self.root / "ack.json").write_text(
            json.dumps({"version": 2, "handled_seq": 0}), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "version must be 1"):
            self.open_session()

    def test_an_ack_file_cannot_be_ahead_of_the_action_log(self):
        (self.root / "ack.json").write_text(
            json.dumps({"version": 1, "handled_seq": 1}), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "ahead of produced seq 0"):
            self.open_session()

    def test_snapshot_names_the_action_and_handled_cursors_once(self):
        self.session.act(None, "next", "")

        snapshot = self.session.snapshot()
        self.assertEqual(snapshot["seq"], 1)
        self.assertEqual(snapshot["handled_seq"], 0)
        self.assertNotIn("produced_seq", snapshot)


class LettingGo(SessionTest):
    """A park used to hold its slot for the full 900s after the client vanished, so
    `listening` lied and one packet bought a thread for a quarter of an hour."""

    def test_a_park_ends_when_the_client_is_gone(self):
        started = time.monotonic()
        self.assertIsNone(self.session.wait(30.0, gone=lambda: True))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_it_stops_listening_once_the_client_is_gone(self):
        self.session.wait(30.0, gone=lambda: True)
        self.assertFalse(self.session.snapshot()["listening"])

    def test_a_live_client_still_waits_the_whole_timeout(self):
        started = time.monotonic()
        self.assertIsNone(self.session.wait(0.3, gone=lambda: False))
        self.assertGreaterEqual(time.monotonic() - started, 0.3)

    def test_an_action_still_wins_over_the_liveness_check(self):
        woke = []
        waiter = threading.Thread(
            target=lambda: woke.append(self.session.wait(30.0, gone=lambda: False)))
        waiter.start()
        time.sleep(0.05)
        self.session.act(None, "next", "")
        waiter.join(10.0)
        self.assertEqual(woke[0]["action"], "next")


class StatusSize(SessionTest):
    def test_a_long_status_text_is_capped(self):
        """Every subscriber keeps a copy of each event in an unbounded queue."""
        stored = self.session.set_status({"phase": "working", "text": "x" * 60000})
        self.assertEqual(len(stored["text"]), serve.MAX_STATUS_TEXT)


class ATornLog(SessionTest):
    """ENOSPC or a power loss during the append leaves a half-written last line.
    Refusing to parse it stranded the session the log exists to preserve."""

    TORN = '{"seq": 1, "n": null, "action": "next", "note": ""}\n{"seq": 2, "n'

    def tear(self):
        (self.root / "decisions.jsonl").write_text(self.TORN, encoding="utf-8")

    def test_the_readable_records_still_load(self):
        self.tear()
        session = self.open_session()
        self.assertEqual([r["seq"] for r in session.records()], [1])
        self.assertEqual(session.seq, 1)

    def test_a_park_answers_instead_of_raising(self):
        """A live server used to answer every /await with a 500 once this landed."""
        self.tear()
        session = self.open_session()
        session.ack(1)
        self.assertIsNone(session.wait(0.05))

    def test_the_next_decision_does_not_fuse_onto_it(self):
        """The torn line stays where it is. What matters is that the next record gets
        a line of its own, rather than being appended onto the stump and lost too."""
        self.tear()
        session = self.open_session()
        session.act(None, "next", "")
        self.assertEqual([r["seq"] for r in session.records()], [1, 2])
        lines = (self.root / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[1], '{"seq": 2, "n')
        self.assertEqual(json.loads(lines[2])["seq"], 2)

    def test_a_log_ending_cleanly_gains_no_blank_line(self):
        session = self.open_session()
        session.act(None, "next", "")
        session.act(None, "next", "")
        self.assertNotIn(
            "\n\n", (self.root / "decisions.jsonl").read_text(encoding="utf-8"))


class Watching(SessionTest):
    """The watcher is what makes a beat the agent wrote straight to disk, which is
    every beat, show up on a page nobody told about it.

    Driven a reading at a time rather than by a thread and a sleep, because the loop
    has no baseline until its first reading lands. A test that raced that tick saw no
    event, and could not tell that apart from a watcher that was broken.
    """

    def setUp(self):
        super().setUp()
        self.channel = self.session.subscribe()

    def readings(self, *between):
        """Take a baseline reading, then one more after each callable runs.

        The loop asks `stop.wait` whether to keep going, so standing in for it is what
        turns an interval into a step. The real body runs untouched.
        """
        steps, taken = list(between), 0

        def wait(_interval):
            nonlocal taken
            taken += 1
            if taken == 1:
                return False
            if not steps:
                return True
            steps.pop(0)()
            return False

        self.session.stop.wait = wait
        try:
            self.session.watch()
        finally:
            del self.session.stop.wait

    def test_a_beat_written_behind_its_back_still_reaches_the_page(self):
        self.readings(lambda: self.put(dict(FLAG, state="dropped")))
        self.assertEqual(self.channel.get_nowait()["rev"], self.session.fingerprint())

    def test_a_reading_that_matches_the_last_one_publishes_nothing(self):
        """A watcher that published every time it looked would swap the body out from
        under the reviewer twice a second."""
        self.readings(lambda: None)
        self.assertTrue(self.channel.empty())

    def test_a_file_that_vanishes_does_not_take_the_watcher_with_it(self):
        """It runs on a daemon thread nobody joins, so an exception here is silent:
        the page simply stops updating for the rest of the session."""
        kept = (self.root / "session.json").read_bytes()
        self.readings(
            lambda: (self.root / "session.json").unlink(),
            lambda: (self.root / "session.json").write_bytes(kept),
        )
        self.assertIsNotNone(self.channel.get_nowait())


class HotReload(SessionTest):
    """Editing the renderer mid-session used to do nothing while the CSS reloaded on
    every request, which is a confusing pair of rules to hold in your head at once."""

    def setUp(self):
        super().setUp()
        self.file = self.root / "render.py"
        # LIFO, so RENDERER is the real one again before the cache is cleared and the
        # next test to render reloads it.
        self.addCleanup(self.forget)
        patcher = mock.patch.object(serve, "RENDERER", self.file)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.forget()

    def forget(self):
        serve._renderer = serve._renderer_mtime = None

    def test_an_untouched_renderer_is_not_re_executed(self):
        self.file.write_text("VALUE = 1\n", encoding="utf-8")
        first = serve.rr()
        self.assertEqual(first.VALUE, 1)
        self.assertIs(serve.rr(), first)

    def test_an_edit_takes_effect_without_a_restart(self):
        self.file.write_text("VALUE = 1\n", encoding="utf-8")
        serve.rr()
        self.file.write_text("VALUE = 2\n", encoding="utf-8")
        self.assertEqual(serve.rr().VALUE, 2)

    def test_an_edit_that_moves_neither_the_mtime_nor_the_size_still_lands(self):
        """Two same-length edits inside one second are ordinary while iterating on the
        tool itself, and stat alone cannot tell them apart."""
        self.file.write_text("VALUE = 1\n", encoding="utf-8")
        was = self.file.stat()
        serve.rr()
        self.file.write_text("VALUE = 2\n", encoding="utf-8")
        os.utime(self.file, ns=(was.st_atime_ns, was.st_mtime_ns))
        self.assertEqual(self.file.stat().st_mtime_ns, was.st_mtime_ns)
        self.assertEqual(serve.rr().VALUE, 2)


class PathParsing(unittest.TestCase):
    """The route comes off the raw path by hand and decides what a request reaches."""

    def handler(self, path):
        handler = serve.Handler.__new__(serve.Handler)
        handler.path = path
        return handler

    def test_a_query_string_does_not_become_part_of_the_route(self):
        self.assertEqual(self.handler("/await?after=3").route(), "/await")

    def test_a_trailing_slash_is_the_same_route(self):
        self.assertEqual(self.handler("/state/").route(), "/state")

    def test_the_root_survives_being_stripped(self):
        self.assertEqual(self.handler("/").route(), "/")


class Served(SessionTest):
    """A live server on an ephemeral port, plus the three ways to talk to it."""

    def setUp(self):
        super().setUp()
        handler = type("Handler", (serve.Handler,), {"session": self.session})
        self.httpd = serve.Server(("127.0.0.1", 0), handler)
        # Poll faster than the 0.5s default purely so teardown does not dominate the
        # suite: shutdown() waits for serve_forever to come round again.
        thread = threading.Thread(target=self.httpd.serve_forever, args=(0.01,), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5.0)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.port = self.httpd.server_address[1]
        self.url = "http://127.0.0.1:%d" % self.port

    def post(self, route, body):
        request = urllib.request.Request(
            self.url + route, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as err:
            with err:
                return err.code, err.read().decode()

    def get(self, route):
        try:
            with urllib.request.urlopen(self.url + route, timeout=5) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as err:
            with err:
                return err.code, err.read().decode()

    def raw(self, request):
        """Shapes urllib will not send, like a Content-Length that is not a number."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            sock.sendall(request.encode())
            return sock.recv(4096).decode(errors="replace").split("\r\n")[0]
        finally:
            sock.close()


class Requests(Served):
    def test_an_accept_over_http_resolves_the_flag(self):
        status, body = self.post("/act", {"n": 1, "action": "accept", "note": "yes"})
        action = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(action["seq"], 1)
        self.assertEqual(set(action), set(serve.ACTION_FIELDS))
        self.assertEqual(self.decisions()[0]["delivery_version"], serve.CURSOR_VERSION)
        self.assertEqual(self.beat(1)["state"], "accepted")

    def test_a_decide_over_http_resolves_the_flag(self):
        status, _body = self.post("/act", {"n": 1, "action": "decide", "note": "as is"})
        self.assertEqual(status, 200)
        self.assertEqual(self.beat(1)["state"], "decided")

    def test_an_ack_over_http_advances_the_handled_cursor(self):
        self.session.act(None, "next", "")

        status, body = self.post("/ack", {"seq": 1})

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"handled_seq": 1})
        self.assertEqual(json.loads(self.get("/state")[1])["handled_seq"], 1)

    def test_an_ack_cannot_skip_the_oldest_unhandled_action(self):
        self.session.act(None, "next", "")
        self.session.act(None, "skip", "")

        status, body = self.post("/ack", {"seq": 2})

        self.assertEqual(status, 400)
        self.assertIn("oldest unhandled action is 1", body)

    def test_an_ack_requires_a_non_negative_integer(self):
        self.session.act(None, "next", "")
        for seq in (None, True, "1", 1.5, -1):
            with self.subTest(seq=seq):
                status, body = self.post("/ack", {"seq": seq})
                self.assertEqual(status, 400)
                self.assertIn("seq must be a non-negative integer", body)

    def test_a_content_length_that_is_not_a_number_answers_400(self):
        self.assertIn("400", self.raw(
            "POST /status HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: abc\r\n\r\n"))

    def test_a_json_body_that_is_not_an_object_answers_400(self):
        for route in ("/status", "/act", "/ack"):
            with self.subTest(route=route):
                self.assertEqual(self.post(route, [])[0], 400)

    def test_a_negative_content_length_answers_400(self):
        """It slipped past a ceiling-only guard, and read(-1) then drained until the
        client felt like closing, after which the request ran anyway."""
        self.assertIn("400", self.raw(
            "POST /status HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: -1\r\n\r\n"))

    def test_an_oversized_body_answers_413(self):
        self.assertIn("413", self.raw(
            "POST /act HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: %d\r\n\r\n"
            % (serve.MAX_BODY + 1)))

    def test_an_unknown_action_answers_400(self):
        status, body = self.post("/act", {"action": "bogus"})
        self.assertEqual(status, 400)
        self.assertIn("action must be one of", body)

    def test_a_beat_number_that_is_not_a_number_answers_400(self):
        self.assertEqual(self.post("/act", {"n": "abc", "action": "accept"})[0], 400)

    def test_a_beat_that_does_not_exist_answers_400(self):
        status, body = self.post("/act", {"n": 99, "action": "accept"})
        self.assertEqual(status, 400)
        self.assertIn("no beat 99", body)

    def test_status_round_trips(self):
        status, body = self.post("/status", {"phase": "parked", "text": "waiting on you"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["phase"], "parked")
        self.assertEqual(json.loads(self.get("/state")[1])["status"]["phase"], "parked")

    def test_the_live_page_carries_the_controls(self):
        status, page = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn('data-action="accept"', page)

    def test_state_reports_the_beats_it_found(self):
        self.assertEqual(json.loads(self.get("/state")[1])["beats"], 2)

    def test_an_unknown_route_answers_404(self):
        self.assertEqual(self.get("/nope")[0], 404)

    def test_a_traversing_path_answers_404_without_opening_anything(self):
        self.assertEqual(self.get("/../../../etc/passwd")[0], 404)


class Forgery(Served):
    """Binding loopback is not authentication. Both attacks reach a local port
    from a page the reviewer merely has open, so both are shapes urllib will not
    send and every one of these goes over a raw socket."""

    def send_raw(self, method, route, headers, body=""):
        lines = ["%s %s HTTP/1.1" % (method, route)]
        lines += ["%s: %s" % kv for kv in headers.items()]
        if body:
            lines.append("Content-Length: %d" % len(body))
        return self.raw("\r\n".join(lines) + "\r\n\r\n" + body)

    def act_body(self):
        return json.dumps({"n": 1, "action": "accept", "note": "not the reviewer"})

    def test_a_rebound_host_cannot_read_the_session(self):
        """DNS rebinding: the browser sends the name it navigated to, so refusing
        anything but loopback is what makes the read endpoints unreadable."""
        for route in ("/", "/state", "/fragment"):
            with self.subTest(route=route):
                answer = self.send_raw("GET", route, {"Host": "evil.example.com"})
                self.assertIn("403", answer)

    def test_a_missing_host_is_refused(self):
        self.assertIn("403", self.send_raw("GET", "/state", {}))

    def test_the_walk_reads_with_a_loopback_host(self):
        for host in ("127.0.0.1:%d" % self.port, "localhost:%d" % self.port):
            with self.subTest(host=host):
                self.assertIn("200", self.send_raw("GET", "/state", {"Host": host}))

    def test_a_cross_origin_post_cannot_resolve_a_flag(self):
        """A CORS simple request needs no preflight, so text/parse checks are not a
        defense; the Origin is what gives it away."""
        answer = self.send_raw(
            "POST", "/act",
            {"Host": "127.0.0.1:%d" % self.port, "Origin": "https://evil.example",
             "Content-Type": "text/plain;charset=UTF-8"},
            self.act_body(),
        )
        self.assertIn("403", answer)
        self.assertEqual(self.beat(1)["state"], "flag")
        self.assertEqual(self.decisions(), [])

    def test_a_cross_origin_post_cannot_spoof_the_status(self):
        answer = self.send_raw(
            "POST", "/status",
            {"Host": "127.0.0.1:%d" % self.port, "Origin": "https://evil.example",
             "Content-Type": "text/plain"},
            json.dumps({"phase": "parked", "text": "safe to click"}),
        )
        self.assertIn("403", answer)
        self.assertEqual(self.session.status["phase"], "starting")

    def test_a_cross_origin_post_cannot_acknowledge_an_action(self):
        self.session.act(None, "next", "")
        answer = self.send_raw(
            "POST", "/ack",
            {"Host": "127.0.0.1:%d" % self.port, "Origin": "https://evil.example",
             "Content-Type": "text/plain"},
            json.dumps({"seq": 1}),
        )
        self.assertIn("403", answer)
        self.assertEqual(self.session.handled_seq, 0)

    def test_the_page_resolves_a_flag_from_its_own_origin(self):
        host = "127.0.0.1:%d" % self.port
        answer = self.send_raw(
            "POST", "/act",
            {"Host": host, "Origin": "http://" + host,
             "Content-Type": "application/json"},
            self.act_body(),
        )
        self.assertIn("200", answer)
        self.assertEqual(self.beat(1)["state"], "accepted")

    def test_the_walk_acts_with_no_origin_at_all(self):
        """curl sends none, and the agent drives the same routes the page does."""
        self.assertEqual(self.post("/act", {"n": 1, "action": "accept"})[0], 200)


class Streaming(Served):
    """/events is the whole reason the page never polls: both directions of the walk
    land on it. Every assertion here is something the reviewer would otherwise have
    to reload the page to find out."""

    def stream(self):
        """A raw socket, because urllib wants a response that ends. makefile holds a
        reference of its own, so hanging up means closing both."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        fh = sock.makefile("rb")
        self.addCleanup(sock.close)
        self.addCleanup(fh.close)
        sock.sendall(b"GET /events HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        return sock, fh

    def frames(self, fh, count=1):
        """The next `count` data frames, each read up to its blank terminator so the
        stream is left on a boundary. Reading one proves the subscription exists,
        which is what keeps the tests below from racing their own setup."""
        out = []
        while len(out) < count:
            line = fh.readline()
            if not line:
                break
            if line.startswith(b"data: "):
                out.append(json.loads(line[6:]))
                fh.readline()
        return out

    def test_a_new_stream_opens_with_the_state_as_it_stands(self):
        """The page is built server-side and then goes live, so a first frame that
        never came would leave the controls disabled with nothing to explain it."""
        first = self.frames(self.stream()[1])[0]
        self.assertEqual(first["seq"], 0)
        self.assertEqual(first["status"]["phase"], "starting")
        self.assertFalse(first["listening"])

    def test_an_action_reaches_the_page_without_it_asking(self):
        fh = self.stream()[1]
        self.frames(fh)
        self.session.act(1, "accept", "yes")
        self.assertEqual(self.frames(fh)[0]["seq"], 1)

    def test_what_the_agent_is_doing_reaches_the_page(self):
        """The half files alone cannot express: applying an accept, running tests and
        parked all look identical on disk."""
        fh = self.stream()[1]
        self.frames(fh)
        self.post("/status", {"phase": "working", "text": "running tests"})
        self.assertEqual(self.frames(fh)[0]["status"]["text"], "running tests")

    def test_both_edges_of_a_park_reach_the_page(self):
        """`listening` is what the page swaps its away copy on, and it is inferred
        from nothing else: an agent that died mid-walk leaves its last status standing."""
        fh = self.stream()[1]
        self.frames(fh)
        waiter = threading.Thread(target=lambda: self.session.wait(5.0))
        waiter.start()
        self.assertTrue(self.frames(fh)[0]["listening"])
        self.session.act(None, "next", "")
        waiter.join(5.0)
        self.assertFalse(self.frames(fh, 2)[-1]["listening"])

    def test_a_silent_stream_still_gets_a_heartbeat(self):
        """A long beat is minutes of silence, and an idle connection is exactly what a
        browser or a proxy will close underneath it."""
        with mock.patch.object(serve, "HEARTBEAT", 0.05):
            fh = self.stream()[1]
            self.frames(fh)
            self.assertEqual(fh.readline(), b": ping\n")

    def test_a_page_that_goes_away_stops_being_a_subscriber(self):
        """Every subscriber holds an unbounded queue, so a leak here is a session
        accumulating a copy of every event for a tab that closed hours ago."""
        sock, fh = self.stream()
        self.frames(fh)
        self.assertEqual(len(self.session.subscribers), 1)
        fh.close()
        sock.close()
        for _ in range(200):
            self.session.publish()
            if not self.session.subscribers:
                break
            time.sleep(0.01)
        self.assertEqual(self.session.subscribers, [])


class Awaiting(Served):
    """The route the walk itself lives on. Session.wait is covered directly, but
    nothing had run the route that decides which action it is told about."""

    def test_it_answers_with_the_action_that_landed(self):
        self.session.act(1, "accept", "yes")
        status, body = self.get("/await")
        action = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(action["action"], "accept")
        self.assertEqual(set(action), set(serve.ACTION_FIELDS))

    def test_a_caller_cursor_cannot_skip_an_unhandled_action(self):
        self.session.act(None, "next", "")

        self.assertEqual(json.loads(self.get("/await?after=1")[1])["action"], "next")

    def test_it_redelivers_until_ack_then_exposes_the_next_action(self):
        self.session.act(None, "next", "")
        self.session.act(None, "skip", "")

        self.assertEqual(json.loads(self.get("/await")[1])["action"], "next")
        self.assertEqual(json.loads(self.get("/await")[1])["action"], "next")
        self.post("/ack", {"seq": 1})
        self.assertEqual(json.loads(self.get("/await")[1])["action"], "skip")

    def test_an_ack_that_finishes_before_wait_starts_cannot_redeliver(self):
        self.session.act(None, "next", "")
        self.session.act(None, "skip", "")
        original_wait = self.session.wait
        entered = threading.Barrier(2)
        release = threading.Event()

        def delayed_wait(*args, **kwargs):
            entered.wait(5.0)
            if not release.wait(5.0):
                raise RuntimeError("test did not release await")
            return original_wait(*args, **kwargs)

        self.session.wait = delayed_wait
        self.addCleanup(self.session.__dict__.pop, "wait", None)
        answers = []
        walk = threading.Thread(target=lambda: answers.append(self.get("/await")))
        walk.start()
        entered.wait(5.0)
        try:
            self.assertEqual(self.post("/ack", {"seq": 1})[0], 200)
        finally:
            release.set()
        walk.join(10.0)

        self.assertFalse(walk.is_alive())
        self.assertEqual(len(answers), 1)
        self.assertEqual(json.loads(answers[0][1])["seq"], 2)

    def test_nothing_new_answers_a_timeout_rather_than_hanging_up(self):
        """The walk is told a timeout means nobody acted, and reparks. A dropped
        connection instead would end the walk."""
        with mock.patch.object(serve, "AWAIT_TIMEOUT", 0.05):
            self.assertEqual(json.loads(self.get("/await")[1]), {"timeout": True})

    def test_a_park_wakes_the_moment_the_page_acts(self):
        with mock.patch.object(serve, "AWAIT_TIMEOUT", 10.0):
            answers = []
            walk = threading.Thread(
                target=lambda: answers.append(self.get("/await")))
            walk.start()
            time.sleep(0.1)
            self.post("/act", {"n": 1, "action": "accept", "note": "yes"})
            walk.join(15.0)
        self.assertEqual(json.loads(answers[0][1])["action"], "accept")
        self.assertEqual(self.beat(1)["state"], "accepted")


class Fragment(Served):
    """What the page swaps in on every change. It has to keep the controls, or the
    page goes read-only after the first thing that happens on it."""

    def test_it_carries_the_beats_and_the_controls(self):
        status, body = self.get("/fragment")
        self.assertEqual(status, 200)
        self.assertIn('data-action="accept"', body)
        self.assertIn('data-n="1"', body)

    def test_it_is_a_fragment_and_not_a_page(self):
        body = self.get("/fragment")[1]
        self.assertNotIn("<title>", body)
        self.assertNotIn("<style>", body)

    def test_it_is_exactly_what_the_page_already_holds(self):
        """The two are rendered by separate calls, and any disagreement shows up as
        the body flickering into something else on the first swap."""
        self.assertIn(self.get("/fragment")[1], self.get("/")[1])

    def test_it_shows_the_call_that_was_just_made(self):
        self.post("/act", {"n": 1, "action": "accept", "note": "yes, pin it"})
        body = self.get("/fragment")[1]
        self.assertIn("s-acc", body)
        self.assertIn("yes, pin it", body)


class QuietHangUps(unittest.TestCase):
    """The terminal belongs to the walk, the same rule log_message follows. Closing
    the tab, or a park whose page went away, put a full stack trace in front of the
    reviewer, from a keep-alive read that was never going to succeed."""

    def through(self, err):
        server = serve.Server.__new__(serve.Server)
        captured = io.StringIO()
        try:
            raise err
        except type(err):
            with contextlib.redirect_stderr(captured):
                server.handle_error(None, ("127.0.0.1", 1))
        return captured.getvalue()

    def test_a_client_hanging_up_says_nothing(self):
        for err in (ConnectionResetError(), BrokenPipeError(), socket.timeout()):
            with self.subTest(err=type(err).__name__):
                self.assertEqual(self.through(err), "")

    def test_a_real_fault_still_reaches_the_terminal(self):
        self.assertIn("ValueError", self.through(ValueError("a real bug")))


class Lifecycle(unittest.TestCase):
    """main() is unreachable in-process, and the skill depends on all of it."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_an_offline_action_survives_restart_until_it_is_acknowledged(self):
        (self.root / "beats").mkdir()
        (self.root / "session.json").write_text(
            json.dumps({"repo": "acme/widget"}), encoding="utf-8")

        def start():
            proc = subprocess.Popen(
                [sys.executable, str(SCRIPTS / "serve.py"), str(self.root)],
                stdout=subprocess.PIPE, text=True,
            )
            self.addCleanup(proc.stdout.close)
            self.addCleanup(proc.kill)
            return proc, proc.stdout.readline().strip()

        def stop(proc):
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)

        first, url = start()
        request = urllib.request.Request(
            url + "/act", data=b'{"action":"next"}', method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(json.loads(response.read())["seq"], 1)
        stop(first)

        second, url = start()
        with urllib.request.urlopen(url + "/state", timeout=5) as response:
            state = json.loads(response.read())
        self.assertEqual((state["seq"], state["handled_seq"]), (1, 0))
        with urllib.request.urlopen(url + "/await", timeout=5) as response:
            self.assertEqual(json.loads(response.read())["action"], "next")
        request = urllib.request.Request(
            url + "/ack", data=b'{"seq":1}', method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(json.loads(response.read()), {"handled_seq": 1})
        stop(second)

        third, url = start()
        with urllib.request.urlopen(url + "/state", timeout=5) as response:
            self.assertEqual(json.loads(response.read())["handled_seq"], 1)
        stop(third)

    def test_it_prints_a_url_writes_serve_json_and_cleans_up_on_sigterm(self):
        (self.root / "beats").mkdir()
        (self.root / "session.json").write_text(
            json.dumps({"repo": "acme/widget"}), encoding="utf-8")

        proc = subprocess.Popen(
            [sys.executable, str(SCRIPTS / "serve.py"), str(self.root)],
            stdout=subprocess.PIPE, text=True,
        )
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.kill)
        url = proc.stdout.readline().strip()
        self.assertTrue(url.startswith("http://127.0.0.1:"), url)

        served = json.loads((self.root / "serve.json").read_text(encoding="utf-8"))
        self.assertEqual(served["url"], url)
        self.assertEqual(served["pid"], proc.pid)

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertFalse((self.root / "serve.json").exists())

    def test_a_sigterm_racing_startup_still_cleans_up(self):
        """The handler used to be armed after serve.json was written and the URL
        printed, so a SIGTERM in that window took the default disposition, exited 143,
        and left the file behind for the next walk to read and curl. Repeated because
        the window is small: CI hit it, one local run in forty hit it."""
        (self.root / "beats").mkdir()
        (self.root / "session.json").write_text(
            json.dumps({"repo": "acme/widget"}), encoding="utf-8")

        for attempt in range(6):
            with self.subTest(attempt=attempt):
                proc = subprocess.Popen(
                    [sys.executable, str(SCRIPTS / "serve.py"), str(self.root)],
                    stdout=subprocess.PIPE, text=True,
                )
                self.addCleanup(proc.stdout.close)
                self.addCleanup(proc.kill)
                proc.stdout.readline()
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=10)
                self.assertEqual(proc.returncode, 0)
                self.assertFalse((self.root / "serve.json").exists())

    def test_a_directory_with_no_session_exits_1(self):
        done = subprocess.run(
            [sys.executable, str(SCRIPTS / "serve.py"), str(self.root)],
            capture_output=True, text=True,
        )
        self.assertEqual(done.returncode, 1)
        self.assertIn("no session.json", done.stderr)

    def test_a_usage_error_exits_1_like_the_header_says(self):
        """argparse spends 2 on this, and 2 is the code the other two scripts use for
        something the walk is told to carry on after."""
        done = subprocess.run(
            [sys.executable, str(SCRIPTS / "serve.py"), "--bogus", str(self.root)],
            capture_output=True, text=True,
        )
        self.assertEqual(done.returncode, 1)
        self.assertIn("serve:", done.stderr)


class OneServerPerSession(unittest.TestCase):
    """Two live servers on one directory both answered a racing accept and drop, and
    both wrote a record numbered seq 1. It is the race `act` holds a lock to prevent,
    happening across processes where a lock cannot reach."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "beats").mkdir()
        (self.root / "session.json").write_text(
            json.dumps({"repo": "acme/widget"}), encoding="utf-8")

    def start(self):
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPTS / "serve.py"), str(self.root)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        # Reversed, so it reads as kill then reap. Killing alone leaves a zombie and
        # the whole suite runs under -W error::ResourceWarning.
        self.addCleanup(proc.wait)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.stderr.close)
        self.addCleanup(proc.kill)
        return proc

    def second(self):
        """A second start, run to completion. One that does not refuse serves forever,
        so the timeout stands in for the assertion rather than hanging the suite."""
        try:
            return subprocess.run(
                [sys.executable, str(SCRIPTS / "serve.py"), str(self.root)],
                capture_output=True, text=True, timeout=15,
            )
        except subprocess.TimeoutExpired:
            self.fail("the second server did not refuse, it started serving")

    def test_a_second_start_refuses_and_names_the_one_already_running(self):
        url = self.start().stdout.readline().strip()
        done = self.second()
        self.assertEqual(done.returncode, 1)
        self.assertIn(url, done.stderr)

    def test_the_running_server_is_left_exactly_as_it_was(self):
        """The refusing process shares the directory, so cleaning up on its way out
        would take the live server's URL down with it."""
        url = self.start().stdout.readline().strip()
        self.second()
        served = json.loads((self.root / "serve.json").read_text(encoding="utf-8"))
        self.assertEqual(served["url"], url)
        with urllib.request.urlopen(url + "/state", timeout=5) as answer:
            self.assertEqual(answer.status, 200)

    def test_a_serve_json_left_behind_by_a_kill_does_not_block_the_next_walk(self):
        """SIGKILL runs no cleanup, so the file outlives the server. Refusing on the
        file alone would strand the session it exists to describe."""
        first = self.start()
        first.stdout.readline()
        first.kill()
        first.wait(timeout=10)
        self.assertTrue((self.root / "serve.json").exists())
        self.assertTrue(self.start().stdout.readline().startswith("http://127.0.0.1:"))

    def test_a_serve_json_that_will_not_parse_does_not_block_a_start(self):
        (self.root / "serve.json").write_text("{ half written", encoding="utf-8")
        self.assertTrue(self.start().stdout.readline().startswith("http://127.0.0.1:"))

    def test_a_url_nobody_answers_does_not_block_a_start(self):
        """A pid outlives the process that owned it and can be handed to something
        unrelated, which is why the proof is an answer and not a pid."""
        (self.root / "serve.json").write_text(
            json.dumps({"url": "http://127.0.0.1:9", "pid": os.getpid()}), encoding="utf-8")
        self.assertTrue(self.start().stdout.readline().startswith("http://127.0.0.1:"))


if __name__ == "__main__":
    unittest.main()
