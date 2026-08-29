"""Hermetic process fixtures for composed Underwrite workflow tests."""

import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "underwrite" / "scripts"


FAKE_GH = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path


path = Path(os.environ["UNDERWRITE_FAKE_GH_STATE"])
state = json.loads(path.read_text(encoding="utf-8"))
args = sys.argv[1:]
state.setdefault("calls", []).append(args)


def save():
    path.write_text(
        json.dumps(state, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )


if not args or args[0] != "api":
    save()
    sys.exit("fake gh only supports api")

if args == ["api", "user", "--jq", ".login"]:
    save()
    print(state["actor"])
    raise SystemExit(0)

pr_endpoint = "repos/acme/widget/pulls/7"
reviews_endpoint = pr_endpoint + "/reviews"

if args == [
    "api",
    "-H",
    "Accept: application/vnd.github+json",
    pr_endpoint,
]:
    save()
    print(json.dumps(state["metadata"]))
    raise SystemExit(0)

if (
    len(args) == 6
    and args[:4] == ["api", reviews_endpoint, "--method", "POST"]
    and args[4] == "--input"
):
    source = Path(args[5])
    payload = json.loads(source.read_text(encoding="utf-8"))
    state.setdefault("post_attempts", []).append(payload)
    mode = state.get("post_mode", "success")
    if mode == "definite_failure":
        save()
        print("review rejected before creation", file=sys.stderr)
        raise SystemExit(1)
    review = {
        "id": len(state.setdefault("reviews", [])) + 1,
        "commit_id": payload["commit_id"],
        "body": payload.get("body", ""),
        "user": {"login": state["actor"]},
        "state": {
            "COMMENT": "COMMENTED",
            "APPROVE": "APPROVED",
            "REQUEST_CHANGES": "CHANGES_REQUESTED",
        }[payload["event"]],
        "html_url": "https://example.test/reviews/%d"
        % (len(state["reviews"]) + 1),
    }
    state["reviews"].append(review)
    if state.get("move_after_post"):
        state["metadata"]["head"]["sha"] = state["moved_head_sha"]
    save()
    if mode == "create_then_fail":
        print("connection lost after creation", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(review))
    raise SystemExit(0)

if args == ["api", "--paginate", "--slurp", reviews_endpoint]:
    reviews = state.get("reviews", [])
    save()
    print(json.dumps([reviews]))
    raise SystemExit(0)

save()
sys.exit("unsupported fake gh arguments: " + repr(args))
'''


def flag(n, claim, line, fix):
    return {
        "n": n,
        "tier": "core",
        "state": "flag",
        "claim": claim,
        "where": f"app.py:{line}",
        "slots": {
            "what": claim,
            "proof": f"app.py:{line}",
            "risk": f"{claim} can fail at runtime",
            "fix": fix,
        },
    }


class WorkflowCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="underwrite-workflow-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def run_command(
        self, command, *, cwd=None, env=None, value=None, expected=0, timeout=30
    ):
        completed = subprocess.run(
            [str(part) for part in command],
            cwd=None if cwd is None else str(cwd),
            env=env,
            input=None if value is None else json.dumps(value),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        self.assertEqual(completed.returncode, expected, completed.stderr)
        return completed

    def script(self, name, *args, env=None, value=None, expected=0):
        return self.run_command(
            [sys.executable, SCRIPTS / name, *args],
            env=env,
            value=value,
            expected=expected,
        )

    def script_json(self, name, *args, env=None, value=None):
        return json.loads(
            self.script(name, *args, env=env, value=value).stdout
        )

    def git(self, *args, cwd=None):
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        environment.update({
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        })
        return self.run_command(["git", *args], cwd=cwd, env=environment).stdout.strip()

    def start_server(self, session):
        process = subprocess.Popen(
            [sys.executable, str(SCRIPTS / "serve.py"), str(session)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self.cleanup_process, process)
        startup = queue.Queue()
        reader = threading.Thread(
            target=lambda: startup.put(process.stdout.readline()), daemon=True
        )
        reader.start()
        try:
            url = startup.get(timeout=10).strip()
        except queue.Empty:
            process.kill()
            process.wait(timeout=10)
            reader.join(timeout=10)
            self.fail(f"server startup timed out: {process.stderr.read()}")
        if not url:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            self.fail(f"server did not start: {process.stderr.read()}")
        return process, url

    def stop_server(self, process):
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr.read())

    def cleanup_process(self, process):
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        if process.stdout and not process.stdout.closed:
            process.stdout.close()
        if process.stderr and not process.stderr.closed:
            process.stderr.close()

    def request(self, url, route, *, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            url + route,
            data=data,
            method="GET" if body is None else "POST",
            headers={} if body is None else {"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = response.read().decode()
                return response.status, json.loads(payload)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read().decode()

    def write_json(self, path, value):
        path.write_text(
            json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def install_fake_gh(self, metadata):
        binary = self.root / "bin"
        binary.mkdir()
        gh = binary / "gh"
        gh.write_text(FAKE_GH, encoding="utf-8")
        gh.chmod(0o755)
        self.gh_state = self.root / "fake-gh.json"
        self.write_json(
            self.gh_state,
            {
                "actor": "reviewer",
                "calls": [],
                "metadata": metadata,
                "post_attempts": [],
                "reviews": [],
            },
        )
        self.gh_env = dict(
            os.environ,
            PATH=f"{binary}{os.pathsep}{os.environ.get('PATH', '')}",
            UNDERWRITE_FAKE_GH_STATE=str(self.gh_state),
        )

    def fake_gh_state(self):
        return json.loads(self.gh_state.read_text(encoding="utf-8"))

    def update_fake_gh(self, **changes):
        state = self.fake_gh_state()
        state.update(changes)
        self.write_json(self.gh_state, state)
