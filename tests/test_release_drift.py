#!/usr/bin/env python3
"""Tests for the release-drift reporter.

The threshold is the whole design, so most of this replays the detector over this
repo's own history: it has to fire on the four cycles that shipped behavior no
install could reach, and stay silent on the eight that did not.
"""
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load():
    spec = importlib.util.spec_from_file_location(
        "release_drift", ROOT / ".github" / "scripts" / "release-drift.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rd = load()


def git(*args, cwd=None):
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True, cwd=cwd
    ).stdout


def shallow():
    return git("rev-parse", "--is-shallow-repository", cwd=ROOT).strip() == "true"


class Synthetic(unittest.TestCase):
    """A built history, so the measurement is exercised without depending on how
    this repo happens to have been released."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.dir.name)
        self.addCleanup(self.dir.cleanup)
        git("init", "--template=", "-b", "main", str(self.repo))
        git("config", "user.email", "drift@example.test", cwd=self.repo)
        git("config", "user.name", "Drift Test", cwd=self.repo)
        git("config", "commit.gpgsign", "false", cwd=self.repo)
        (self.repo / ".claude-plugin").mkdir()
        (self.repo / "skills").mkdir()
        self.publish("0.1.0", "first release")

    def publish(self, version, subject):
        (self.repo / rd.MANIFEST).write_text(
            json.dumps({"name": "underwrite", "version": version}), encoding="utf-8"
        )
        self.commit(subject)

    def ship(self, subject, where="skills/thing.py"):
        path = self.repo / where
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(subject, encoding="utf-8")
        self.commit(subject)

    def commit(self, subject):
        git("add", "-A", cwd=self.repo)
        git("commit", "-n", "-m", subject, cwd=self.repo)

    def verdict(self, **kw):
        cwd = os.getcwd()
        os.chdir(self.repo)
        try:
            return rd.verdict(**kw)
        finally:
            os.chdir(cwd)

    def test_a_clean_release_reports_nothing_unreleased(self):
        state = self.verdict()

        self.assertEqual(state["version"], "0.1.0")
        self.assertEqual(state["changes"], [])
        self.assertFalse(state["drifting"])

    def test_the_count_arm_fires_only_once_the_limit_is_reached(self):
        for n in range(rd.LIMIT - 1):
            self.ship(f"change {n}")
        self.assertFalse(self.verdict()["drifting"])

        self.ship("the one that crosses")
        state = self.verdict()

        self.assertEqual(len(state["changes"]), rd.LIMIT)
        self.assertTrue(state["drifting"])

    def test_the_age_arm_fires_on_a_trickle_the_count_never_catches(self):
        self.ship("one lonely change")
        state = self.verdict()
        self.assertFalse(state["drifting"])

        later = state["changes"][0]["stamp"] + rd.STALE_DAYS * 86400
        state = self.verdict(now=later)

        self.assertEqual(len(state["changes"]), 1)
        self.assertTrue(state["drifting"])

    def test_a_manifest_edit_that_bumps_nothing_is_not_a_release(self):
        """b95dab1 and 3390b2f both did this. Reading them as releases would have
        hidden 1 and 15 unreleased changes."""
        for n in range(rd.LIMIT):
            self.ship(f"change {n}")
        manifest = self.repo / rd.MANIFEST
        manifest.write_text(
            json.dumps({"name": "underwrite", "version": "0.1.0", "description": "new"}),
            encoding="utf-8",
        )
        self.commit("reword the description")

        state = self.verdict()

        self.assertEqual(state["version"], "0.1.0")
        self.assertEqual(len(state["changes"]), rd.LIMIT)
        self.assertTrue(state["drifting"])

    def test_the_release_clears_it(self):
        for n in range(rd.LIMIT):
            self.ship(f"change {n}")
        self.assertTrue(self.verdict()["drifting"])

        self.publish("0.2.0", "Release 0.2.0")
        state = self.verdict()

        self.assertEqual(state["version"], "0.2.0")
        self.assertEqual(state["changes"], [])
        self.assertFalse(state["drifting"])

    def test_a_change_outside_the_published_tree_is_not_drift(self):
        for n in range(rd.LIMIT + 2):
            self.ship(f"test {n}", where=f"tests/test_{n}.py")

        self.assertFalse(self.verdict()["drifting"])

    def test_a_shallow_clone_refuses_rather_than_reporting_zero(self):
        bare = Path(self.dir.name) / "shallow"
        git("clone", "--depth", "1", f"file://{self.repo}", str(bare))
        cwd = os.getcwd()
        os.chdir(bare)
        try:
            with self.assertRaises(rd.DriftError) as caught:
                rd.verdict()
        finally:
            os.chdir(cwd)

        self.assertIn("shallow", str(caught.exception))


@unittest.skipIf(shallow(), "needs full history; check out with fetch-depth 0")
class OverThisRepoHistory(unittest.TestCase):
    """Every cycle this repo has cut, measured the moment before the bump landed.
    Four of them shipped behavior no install could reach: 0.4.0 (DD-2058, 16.6
    days), 0.5.0 (DD-2359), 0.6.0, and 0.7.0 (DD-4040)."""

    EXPECTED = {
        "0.2.0": False,
        "0.3.0": False,
        "0.3.1": False,
        "0.4.0": True,
        "0.5.0": True,
        "0.6.0": True,
        "0.6.1": False,
        "0.6.2": False,
        "0.6.3": False,
        "0.6.4": False,
        "0.6.5": False,
        "0.7.0": True,
    }

    def releases(self):
        found = []
        for sha in git(
            "rev-list", "--first-parent", "HEAD", "--", rd.MANIFEST, cwd=ROOT
        ).split():
            parents = git("rev-list", "--parents", "-1", sha, cwd=ROOT).split()[1:]
            if parents and rd.published_version(sha) != rd.published_version(parents[0]):
                found.append(sha)
        found.reverse()
        return found

    def test_it_fires_on_every_drifted_cycle_and_no_healthy_one(self):
        seen = {}
        for sha in self.releases():
            parent = git("rev-parse", f"{sha}^", cwd=ROOT).strip()
            cut = int(git("log", "-1", "--format=%ct", sha, cwd=ROOT).strip())
            seen[rd.published_version(sha)] = rd.verdict(parent, now=cut)["drifting"]

        self.assertEqual(seen, self.EXPECTED)

    def test_the_limit_sits_in_the_gap_with_margin_on_both_sides(self):
        """Healthy cycles peak at 5 and drifted ones start at 7, so 6 is the only
        limit that neither fires on the heaviest healthy cycle nor sits on the
        boundary of the lightest incident."""
        peaks = sorted(
            len(rd.verdict(git("rev-parse", f"{sha}^", cwd=ROOT).strip(), now=0)["changes"])
            for sha in self.releases()
        )
        quiet = max(n for n in peaks if n < rd.LIMIT)
        firing = min(n for n in peaks if n >= rd.LIMIT)

        self.assertEqual((quiet, firing), (5, 7))
        self.assertLess(quiet, rd.LIMIT)
        self.assertLess(rd.LIMIT, firing)

    def test_main_is_quiet_right_now(self):
        self.assertFalse(rd.verdict()["drifting"], rd.summarize(rd.verdict()))


FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

state = Path(os.environ["FAKE_GH_STATE"])
db = json.loads(state.read_text())
args = sys.argv[1:]
db.setdefault("calls", []).append(args)
out = ""
if args[:2] == ["issue", "list"]:
    out = json.dumps([db["issue"]] if db.get("issue") else [])
elif args[:2] == ["issue", "create"]:
    db["issue"] = {"number": 7, "state": "OPEN"}
    db["body"] = args[args.index("--body") + 1]
elif args[:2] == ["issue", "edit"]:
    db["body"] = args[args.index("--body") + 1]
elif args[:2] == ["issue", "reopen"]:
    db["issue"]["state"] = "OPEN"
elif args[:2] == ["issue", "close"]:
    db["issue"]["state"] = "CLOSED"
elif args[:2] == ["label", "create"]:
    db["labels"] = sorted(set(db.get("labels", []) + [args[2]]))
else:
    sys.exit("fake gh got " + " ".join(args))
state.write_text(json.dumps(db))
sys.stdout.write(out)
"""


class Reconcile(unittest.TestCase):
    """The issue is the whole point of the reporter, so the lifecycle is exercised
    against a stand-in gh rather than left to the first real drift."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        home = Path(self.dir.name)
        fake = home / "gh"
        fake.write_text(FAKE_GH, encoding="utf-8")
        fake.chmod(0o755)
        self.state = home / "state.json"
        self.state.write_text("{}", encoding="utf-8")
        for name, value in (
            ("PATH", f"{home}{os.pathsep}{os.environ['PATH']}"),
            ("FAKE_GH_STATE", str(self.state)),
        ):
            previous = os.environ.get(name)
            os.environ[name] = value
            self.addCleanup(os.environ.__setitem__, name, previous or "")

    def db(self):
        return json.loads(self.state.read_text(encoding="utf-8"))

    def run_reconcile(self, count):
        changes = [
            {"sha": f"{n:040x}", "stamp": 1_700_000_000 + n, "subject": f"change {n}"}
            for n in range(count)
        ]
        return rd.reconcile(
            {
                "version": "9.9.9",
                "released_at": 1_600_000_000,
                "base": "f" * 40,
                "changes": changes,
                "age_days": 1.0,
                "drifting": count >= rd.LIMIT,
            },
            "acme/widget",
        )

    def test_the_label_is_claimed_even_when_there_is_nothing_to_report(self):
        """It needs the same issues: write the report does, so a token that cannot
        report fails on a quiet run rather than during a drift."""
        self.assertEqual(self.run_reconcile(0), "quiet")

        self.assertEqual(self.db()["labels"], [rd.LABEL])

    def test_one_issue_per_episode_then_edits_in_place(self):
        self.assertEqual(self.run_reconcile(rd.LIMIT), "opened")
        self.assertEqual(self.run_reconcile(rd.LIMIT + 1), "updated")

        calls = [c[:2] for c in self.db()["calls"]]

        self.assertEqual(calls.count(["issue", "create"]), 1)
        self.assertIn(f"{rd.LIMIT + 1} changes", self.db()["body"])

    def test_the_release_closes_it_and_returning_drift_reopens_the_same_issue(self):
        self.run_reconcile(rd.LIMIT)
        self.assertEqual(self.run_reconcile(0), "closed")
        self.assertEqual(self.db()["issue"]["state"], "CLOSED")

        self.assertEqual(self.run_reconcile(rd.LIMIT), "reopened")

        calls = [c[:2] for c in self.db()["calls"]]
        self.assertEqual(calls.count(["issue", "create"]), 1)
        self.assertEqual(self.db()["issue"]["state"], "OPEN")

    def test_the_body_names_the_version_and_every_unreleased_commit(self):
        self.run_reconcile(rd.LIMIT)
        text = self.db()["body"]

        self.assertIn("9.9.9", text)
        for n in range(rd.LIMIT):
            self.assertIn(f"change {n}", text)


class WhatItWatches(unittest.TestCase):
    def test_the_measured_tree_still_covers_what_the_plugin_declares(self):
        """PUBLISHED is a tuple, so a second entrypoint key in plugin.json would
        narrow the detector silently."""
        plugin = json.loads((ROOT / rd.MANIFEST).read_text(encoding="utf-8"))
        paths = [v for k, v in plugin.items() if isinstance(v, str) and v.startswith("./")]

        self.assertEqual(paths, ["./skills/"])
        for declared in paths:
            self.assertIn(declared.strip("./"), rd.PUBLISHED)

    def test_the_reporter_never_runs_on_a_fork_controlled_event(self):
        workflow = (ROOT / ".github" / "workflows" / "release-drift.yml").read_text(
            encoding="utf-8"
        )
        triggers, inside = [], False
        for line in workflow.splitlines():
            if line.startswith("on:"):
                inside = True
            elif inside and line[:1] not in ("", " ", "#"):
                break
            elif inside and not line.lstrip().startswith("#"):
                triggers.append(line)

        self.assertIn("issues: write", workflow)
        self.assertEqual(
            sorted(t.strip().rstrip(":") for t in triggers if t.startswith("  ") and not t.startswith("   ")),
            ["push", "schedule", "workflow_dispatch"],
        )

    def test_the_detector_is_not_measuring_itself(self):
        """It lives outside the published tree, so its own commits cannot read as
        behavior waiting on a release."""
        for path in (
            ".github/scripts/release-drift.py",
            ".github/workflows/release-drift.yml",
            "tests/test_release_drift.py",
        ):
            self.assertFalse(path.startswith(rd.PUBLISHED), path)


if __name__ == "__main__":
    unittest.main()
