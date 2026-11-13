#!/usr/bin/env python3
"""The eval set: cases from real misses and known-clean PRs, a scorer, and the runner's
permission surface. The walk itself needs a model and runs locally through evals/run.py."""

import argparse
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVALS = ROOT / "evals"
sys.path.insert(0, str(EVALS))
sys.path.insert(0, str(ROOT / "skills" / "underwrite" / "scripts"))

import score  # noqa: E402
import session_store  # noqa: E402

spec = importlib.util.spec_from_file_location("eval_run", EVALS / "run.py")
run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run)


class Cases(unittest.TestCase):
    def test_every_case_is_well_formed_and_named_for_its_file(self):
        paths = sorted((EVALS / "cases").glob("*.json"))
        self.assertGreaterEqual(len(paths), 2)
        kinds = set()
        for path in paths:
            with self.subTest(case=path.name):
                case = score.load_case(path)
                self.assertEqual(case["id"], path.stem)
                kinds.add(case["kind"])
        self.assertEqual(kinds, {"miss", "clean"})

    def test_the_skill_names_no_case_it_is_scored_on(self):
        text = (ROOT / "skills" / "underwrite" / "SKILL.md").read_text(encoding="utf-8")
        for path in sorted((EVALS / "cases").glob("*.json")):
            with self.subTest(case=path.stem):
                self.assertNotIn(path.stem, text)

    def test_a_misshapen_case_is_refused(self):
        good = {
            "id": "x", "kind": "miss", "repo": "acme/widget", "number": 1,
            "base_sha": "a" * 40, "head_sha": "b" * 40, "evidence": "e",
            "expected": [{"path": "a.py", "lines": [3], "class": "c", "keywords": ["guard"]}],
        }
        self.assertEqual(score.case_problems(good), [])
        for change, message in (
            ({"kind": "maybe"}, "kind must be"),
            ({"head_sha": "abc"}, "full lowercase SHA"),
            ({"expected": []}, "names what the walk should have flagged"),
            ({"kind": "clean"}, "expects no finding"),
            ({"expected": [{"path": "a.py", "lines": []}]}, "path, lines, class and keywords"),
            ({"expected": [{"path": "a.py", "lines": [3], "class": "c"}]}, "class and keywords"),
            ({"repo": ["acme/widget"]}, "repo must be nonempty text"),
            ({"number": True}, "number must be a positive integer"),
            ({"acceptable": {}}, "acceptable must be a list"),
            ({"acceptable": [{"path": "a.py"}]}, "each acceptable finding has"),
        ):
            with self.subTest(change=change):
                self.assertIn(message, "; ".join(score.case_problems(dict(good, **change))))


class Scoring(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        repo = self.tmp / "repo"
        git = lambda *a: subprocess.run(
            ["git", "-C", str(repo), *a], check=True, capture_output=True, text=True
        ).stdout.strip()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        for key, value in (("user.name", "t"), ("user.email", "t@example.test"), ("commit.gpgsign", "false")):
            git("config", key, value)
        (repo / "a.py").write_text("".join(f"line {n}\n" for n in range(1, 21)), encoding="utf-8")
        git("add", "a.py")
        git("commit", "-q", "-m", "base")
        self.base = git("rev-parse", "HEAD")
        (repo / "a.py").write_text(
            "".join(f"{'CHANGED' if n in (5, 15) else 'line'} {n}\n" for n in range(1, 21)),
            encoding="utf-8",
        )
        git("commit", "-q", "-am", "head")
        self.head = git("rev-parse", "HEAD")
        self.root = self.tmp / "reviews" / "acme-widget-pr7"
        store = session_store.SessionStore(self.root, create=True)
        inputs = self.tmp / "inputs"
        inputs.mkdir()
        (inputs / "pr.diff").write_text(git("diff", f"{self.base}...{self.head}") + "\n", encoding="utf-8")
        (inputs / "pr.json").write_text("{}", encoding="utf-8")
        (inputs / "trusted.json").write_text(
            json.dumps({"version": 1, "base_sha": self.base, "files": []},
                       separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        git("update-ref", "refs/underwrite/base", self.base)
        git("update-ref", "refs/underwrite/head", self.head)
        git("bundle", "create", "-q", str(inputs / "pr.bundle"), "refs/underwrite/base", "refs/underwrite/head")
        store.freeze_target(
            {
                "version": 1, "kind": "github_pr", "repo": "acme/widget", "number": 7,
                "state": "closed", "merged_at": "2026-10-01T00:00:00Z", "base_sha": self.base,
                "head_sha": self.head, "head_repo_id": 1, "head_repo": "acme/widget",
                "head_ref": "f", "merge_base_sha": self.base, "changed_files": 1,
            },
            inputs / "pr.diff", inputs / "pr.json", inputs / "trusted.json", inputs / "pr.bundle",
        )
        store.patch_session({"recommendation": "Eval mode: done.", "eval_complete": True})
        self.store = store

    def case(self, kind="miss", lines=(5,)):
        return {
            "id": "t", "kind": kind, "repo": "acme/widget", "number": 7,
            "base_sha": self.base, "head_sha": self.head, "evidence": "e",
            "expected": [] if kind == "clean" else [
                {"path": "a.py", "lines": list(lines), "class": "c", "keywords": ["guard"]}
            ],
        }

    def flag(self, n, line, **extra):
        return {
            "n": n, "tier": "core", "state": "flag", "proof_kind": "read",
            "claim": extra.pop("claim", "the empty guard is gone"), "where": f"a.py:{line}",
            "slots": {"what": "w", "proof": f"a.py:{line}", "risk": "r", "fix": "f"},
            **extra,
        }

    def test_a_flag_within_the_window_finds_the_miss(self):
        self.store.put_beat(self.flag(1, 7))
        result = score.score(self.case(), self.root)
        self.assertTrue(result["pass"])
        self.assertEqual(result["found"][0]["beat"], 1)
        self.assertEqual(result["false_alarms"], [])

    def test_a_flag_elsewhere_misses_it_and_counts_as_a_false_alarm(self):
        self.store.put_beat(self.flag(1, 15))
        result = score.score(self.case(), self.root)
        self.assertFalse(result["pass"])
        self.assertEqual(len(result["missed"]), 1)
        self.assertEqual([alarm["n"] for alarm in result["false_alarms"]], [1])

    def test_a_demoted_flag_is_no_alarm_on_a_clean_case(self):
        self.store.put_beat({
            "n": 1, "tier": "core", "state": "unverified", "proof_kind": "read", "claim": "c",
            "where": "a.py:5", "slots": {"what": "w", "proof": "a.py:5"},
            "second_read": {"verdict": "disputed", "evidence": "not introduced here"},
        })
        result = score.score(self.case("clean"), self.root)
        self.assertTrue(result["pass"])
        self.assertEqual((result["flags"], result["demoted"]), (0, 1))

    def test_a_nearby_flag_about_something_else_finds_nothing(self):
        self.store.put_beat(self.flag(1, 5, claim="the log line is noisy"))
        result = score.score(self.case(), self.root)
        self.assertEqual(len(result["missed"]), 1)
        self.assertEqual([alarm["n"] for alarm in result["false_alarms"]], [1])

    def test_a_found_miss_with_an_extra_alarm_does_not_pass(self):
        self.store.put_beat(self.flag(1, 5))
        self.store.put_beat(self.flag(2, 15))
        result = score.score(self.case(), self.root)
        self.assertEqual(len(result["found"]), 1)
        self.assertFalse(result["pass"])

    def test_a_provisional_recommendation_without_the_marker_is_unfinished(self):
        self.store.put_beat(self.flag(1, 5))
        self.store.patch_session({"eval_complete": False})
        result = score.score(self.case(), self.root)
        self.assertFalse(result["finished"])
        self.assertFalse(result["pass"])

    def test_a_walk_with_no_recommendation_is_unfinished(self):
        self.store.put_beat(self.flag(1, 5))
        with sqlite3.connect(str(self.root / "session.sqlite3")) as db:
            body = json.loads(db.execute("SELECT body_json FROM session").fetchone()[0])
            body.pop("recommendation")
            db.execute("UPDATE session SET body_json = ?", (json.dumps(body),))
        result = score.score(self.case(), self.root)
        self.assertFalse(result["finished"])
        self.assertFalse(result["pass"])

    def test_a_walk_that_stored_nothing_passes_no_case(self):
        result = score.score(self.case("clean"), self.root)
        self.assertEqual(result["beats"], 0)
        self.assertFalse(result["pass"])

    def test_a_standing_flag_fails_a_clean_case(self):
        self.store.put_beat(self.flag(1, 5))
        self.assertFalse(score.score(self.case("clean"), self.root)["pass"])

    def test_a_session_for_another_base_or_head_is_refused(self):
        for field in ("base_sha", "head_sha"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(score.CaseError, "did not walk this case"):
                    score.score(dict(self.case(), **{field: "c" * 40}), self.root)

    def test_a_dropped_flag_no_longer_stands(self):
        self.store.put_beat(self.flag(1, 5))
        self.store.produce("drop-1", 1, "drop", "not this", reason="wrong")
        result = score.score(self.case("clean"), self.root)
        self.assertEqual(result["flags"], 0)
        self.assertTrue(result["pass"])

    def test_one_flag_answers_for_one_expected_finding(self):
        self.store.put_beat(self.flag(1, 5))
        case = dict(self.case(), expected=[
            {"path": "a.py", "lines": [5], "class": "one", "keywords": ["guard"]},
            {"path": "a.py", "lines": [6], "class": "two", "keywords": ["guard"]},
        ])
        result = score.score(case, self.root)
        self.assertEqual((len(result["found"]), len(result["missed"])), (1, 1))

    def test_a_flag_citing_two_sites_finds_a_miss_at_either(self):
        self.store.put_beat(self.flag(1, 15, where="a.py:15 and a.py:5"))
        result = score.score(self.case(), self.root)
        self.assertTrue(result["pass"])
        self.assertEqual(result["found"][0]["beat"], 1)

    def test_an_acceptable_finding_is_neither_required_nor_an_alarm(self):
        self.store.put_beat(self.flag(1, 15, claim="the race test is flaky"))
        case = dict(self.case("clean"), acceptable=[
            {"path": "a.py", "lines": [15], "class": "weak test", "keywords": ["flaky"]},
        ])
        result = score.score(case, self.root)
        self.assertTrue(result["pass"])
        self.assertEqual(result["false_alarms"], [])
        self.assertEqual([(a["n"], a["class"]) for a in result["acceptable"]], [(1, "weak test")])
        self.assertTrue(score.score(self.case("clean"), self.root)["false_alarms"])

    def test_an_acceptable_finding_does_not_stand_in_for_the_miss(self):
        self.store.put_beat(self.flag(1, 15, claim="the race test is flaky"))
        case = dict(self.case(), acceptable=[
            {"path": "a.py", "lines": [15], "class": "weak test", "keywords": ["flaky"]},
        ])
        result = score.score(case, self.root)
        self.assertFalse(result["pass"])
        self.assertEqual(len(result["missed"]), 1)


class Runner(unittest.TestCase):
    def test_the_walk_runs_in_eval_mode_with_a_narrow_tool_list(self):
        args = argparse.Namespace(claude="claude", max_budget_usd=5.0, model=None, bare=False)
        reviews = Path("/tmp/eval-reviews")
        checkout = Path("/tmp/eval-checkout")
        argv = run.command({"number": 83}, ROOT, reviews, checkout, args)
        self.assertIn("/underwrite:underwrite 83 --eval", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        brief = argv[argv.index("--append-system-prompt") + 1]
        self.assertIn("write absolute paths, never shell variables", brief)
        self.assertIn(str(reviews), brief)
        tools = run.allowed_tools(ROOT / "skills" / "underwrite", reviews, checkout)
        self.assertNotIn("Bash", tools)
        self.assertFalse(set(tools) & {"Read", "Grep", "Glob"})
        self.assertIn("Read(//tmp/eval-checkout/**)", tools)
        writes = [tool for tool in tools if tool.startswith(("Edit", "Write"))]
        self.assertEqual(writes, ["Write(//tmp/eval-reviews/**)", "Edit(//tmp/eval-reviews/**)"])
        bash = [tool for tool in tools if tool.startswith("Bash(")]
        sessionctl = [tool for tool in bash if "sessionctl.py" in tool and not tool.endswith("--help)")]
        self.assertTrue(sessionctl)
        self.assertTrue(all(tool.endswith(f" {reviews}/*)") for tool in sessionctl))
        self.assertFalse([tool for tool in sessionctl if "sessionctl.py *" in tool])
        self.assertFalse([tool for tool in bash if tool.startswith("Bash(mkdir")])

    def test_a_regression_is_a_case_that_stopped_passing_or_a_count_that_rose(self):
        baseline = {
            "summary": {"missed": 0, "false_alarms": 1},
            "results": [{"case": "a", "pass": True}, {"case": "b", "pass": False}],
        }
        same = {"summary": {"missed": 0, "false_alarms": 1},
                "results": [{"case": "a", "pass": True, "finished": True},
                            {"case": "b", "pass": False, "finished": True}]}
        self.assertEqual(run.regressions(baseline, same), [])
        worse = {"summary": {"missed": 1, "false_alarms": 2},
                 "results": [{"case": "a", "pass": False, "finished": True},
                             {"case": "b", "pass": False, "finished": True}]}
        self.assertEqual(len(run.regressions(baseline, worse)), 3)

    def test_an_unscored_case_never_holds_steady(self):
        baseline = {
            "summary": {"missed": 1, "false_alarms": 1},
            "results": [{"case": "a", "pass": True}, {"case": "b", "pass": False},
                        {"case": "c", "pass": False}],
        }
        report = {"summary": {"missed": 0, "false_alarms": 0}, "results": [
            {"case": "a", "pass": True, "finished": True},
            {"case": "b", "error": "walk exited 1; see walk.stderr"},
        ]}
        found = run.regressions(baseline, report)
        self.assertIn("b errored: walk exited 1; see walk.stderr", found)
        self.assertIn("c is in the baseline and was not run", found)
        report["results"][1] = {"case": "b", "pass": False, "finished": False}
        self.assertIn("b did not finish its walk", run.regressions(baseline, report))

    def test_the_skill_documents_the_mode_and_the_scratch_reviews_root(self):
        text = " ".join((ROOT / "skills" / "underwrite" / "SKILL.md").read_text(encoding="utf-8").split())
        self.assertIn("An argument carrying `--eval` is a scored replay, not a review", text)
        self.assertIn("Leave every flag open, deliver nothing, skip Phase 4", text)
        self.assertIn("When `UNDERWRITE_REVIEWS` is set, it replaces `~/.claude/reviews`", text)
        self.assertIn("pass its path instead of piping it", text)
        self.assertIn("`eval_complete: true`", text)
class RepeatedRuns(unittest.TestCase):
    """One walk of a nondeterministic skill is one sample. Several runs of a case pass on
    a majority and report mean counts, so a lone flip does not read as a regression."""

    def setUp(self):
        self.original = run.run_case
        self.addCleanup(setattr, run, "run_case", self.original)

    def scripted(self, *outcomes):
        queue = list(outcomes)
        run.run_case = lambda case, args: dict(queue.pop(0), case=case["id"])

    def args(self, runs):
        return argparse.Namespace(runs=runs)

    def test_a_single_run_keeps_the_result_shape(self):
        self.scripted({"pass": True, "finished": True, "missed": [], "false_alarms": []})
        result = run.repeated({"id": "a"}, self.args(1))
        self.assertNotIn("runs", result)
        self.assertTrue(result["pass"])

    def test_a_case_passes_on_a_majority_with_mean_counts(self):
        alarm = [{"n": 1}]
        self.scripted(
            {"pass": True, "finished": True, "missed": [], "false_alarms": [], "cost_usd": 1.0},
            {"pass": False, "finished": True, "missed": [], "false_alarms": alarm, "cost_usd": 1.5},
            {"pass": True, "finished": True, "missed": [], "false_alarms": [], "cost_usd": 2.0},
        )
        result = run.repeated({"id": "a"}, self.args(3))
        self.assertEqual((result["passes"], result["pass"]), (2, True))
        self.assertEqual(result["pass_rate"], 0.667)
        self.assertEqual(result["false_alarms"], 0.333)
        self.assertEqual(result["cost_usd"], 4.5)
        self.assertEqual(len(result["runs"]), 3)

    def test_a_split_is_not_a_pass_and_an_errored_run_is_reported(self):
        self.scripted(
            {"pass": True, "finished": True, "missed": [], "false_alarms": []},
            {"error": "walk exited 1; see walk.stderr"},
        )
        result = run.repeated({"id": "a"}, self.args(2))
        self.assertFalse(result["pass"])
        self.assertIn("1 of 2 runs errored", result["error"])


class KeepingEvidence(unittest.TestCase):
    """A scored failure is the run worth reading, so its work directory survives."""

    def test_a_failed_walk_keeps_its_work_and_a_pass_cleans_up(self):
        work_root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work_root, True)
        original_command, original_score = run.command, run.score
        self.addCleanup(setattr, run, "command", original_command)
        self.addCleanup(setattr, run, "score", original_score)
        seed = work_root / "seed"
        subprocess.run(["git", "init", "-q", str(seed)], check=True)
        subprocess.run(
            ["git", "-C", str(seed), "-c", "user.name=t", "-c", "user.email=t@e.test",
             "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "b"],
            check=True,
        )
        base = subprocess.run(
            ["git", "-C", str(seed), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
        ).stdout.strip()
        walk = (
            "import os, pathlib; root = pathlib.Path(os.environ['UNDERWRITE_REVIEWS']) / 's';"
            "root.mkdir(); (root / 'session.sqlite3').write_bytes(b''); print('{}')"
        )
        run.command = lambda case, plugin, reviews, checkout, args: [sys.executable, "-c", walk]
        case = {"id": "k", "repo": str(seed), "base_sha": base}
        args = argparse.Namespace(work_root=str(work_root / "work"), keep=False, timeout=60)
        clone = lambda argv, **kw: subprocess.run(
            ["git", "clone", "-q", argv[3], argv[4]] if argv[:3] == ["gh", "repo", "clone"] else argv,
            **kw,
        )
        original_run = run.subprocess.run
        self.addCleanup(setattr, run.subprocess, "run", original_run)
        run.subprocess.run = lambda argv, **kw: clone(argv, **kw) if argv[0] == "gh" else original_run(argv, **kw)
        for passed in (False, True):
            with self.subTest(passed=passed):
                run.score = lambda case, root, passed=passed: {"pass": passed, "finished": True}
                result = run.run_case(case, args)
                self.assertNotIn("error", result)
                self.assertEqual("work" in result, not passed)
                if not passed:
                    self.assertTrue(Path(result["work"]).is_dir())
                    self.assertEqual(Path(result["work"]).parent, (work_root / "work").resolve())


class Provenance(unittest.TestCase):
    def test_a_report_names_the_skill_commit_and_the_settings(self):
        args = argparse.Namespace(model="m", runs=3, bare=True, max_budget_usd=2.0)
        found = run.provenance(args)
        self.assertRegex(found["skill_commit"] or "", r"^[0-9a-f]{40}$")
        self.assertEqual(
            {k: found[k] for k in ("model", "runs", "bare", "max_budget_usd")},
            {"model": "m", "runs": 3, "bare": True, "max_budget_usd": 2.0},
        )
        self.assertIsInstance(found["skill_dirty"], bool)


class Labels(unittest.TestCase):
    def test_the_weak_race_test_flag_on_17_is_acceptable_not_an_alarm(self):
        case = score.load_case(EVALS / "cases" / "underwrite-17-sigterm.json")
        beat = {
            "n": 1, "where": "tests/test_serve.py:434",
            "claim": "the race test passes on the old ordering most runs", "slots": {},
        }
        self.assertTrue(any(score.matches(item, beat) for item in case["acceptable"]))


if __name__ == "__main__":
    unittest.main()
