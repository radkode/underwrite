#!/usr/bin/env python3
"""
Serve an underwrite session so the page and the walk stay in step, both ways.

Page to agent: POST /act carries the reviewer's action, and the agent parks on
/await until one arrives. POST /ack advances the durable consumer cursor only
after the walk has incorporated that action.

Agent to page: POST /status carries what the agent is doing right now, which is
the half that files alone cannot express. "Applying your accept", "running
tests", "parked waiting on you" all look identical on disk.

Both directions land on /events, a Server-Sent Events stream, so the page never
polls. A watcher thread covers writes nobody announced.

Loopback only, and no path is ever taken from a request: every read and write is
a fixed name inside the session directory. Binding loopback is not by itself
authentication, so requests are checked for the two ways a browser reaches a
local port from somewhere else: see Handler.forged.

Exit 1  usage error, or the port could not be bound
"""
import argparse
import json
import os
import queue
import select
import signal
import socket
import sqlite3
import sys
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

HERE = Path(__file__).resolve().parent
RENDERER = HERE / "render-report.py"
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from session_store import ACTIONS, Conflict, SessionStore, StoreError

_renderer = None
_renderer_mtime = None
_renderer_lock = threading.Lock()


def rr():
    """The renderer module, re-executed when its file changes.

    Importing once at startup meant editing the renderer mid-session did nothing
    while CSS hot-reloaded on every request, which is a confusing pair of rules to
    hold in your head when you are iterating on the tool itself.
    """
    global _renderer, _renderer_mtime
    source = RENDERER.read_text(encoding="utf-8")
    stamp = (RENDERER.stat().st_mtime_ns, hash(source))
    with _renderer_lock:
        if _renderer is None or stamp != _renderer_mtime:
            # Compiled here rather than via exec_module: Python invalidates its bytecode
            # cache on source mtime plus size, so two same-length edits inside one second
            # load a stale .pyc.
            module = ModuleType("render_report")
            module.__file__ = str(RENDERER)
            exec(compile(source, str(RENDERER), "exec"), module.__dict__)
            _renderer, _renderer_mtime = module, stamp
    return _renderer

MAX_BODY = 64 * 1024
AWAIT_TIMEOUT = 900.0
# A park sleeps in slices so it can notice the client left between them.
WAIT_SLICE = 5.0
SOCKET_TIMEOUT = 60.0
MAX_STATUS_TEXT = 2000
HEARTBEAT = 20.0
WATCH_INTERVAL = 0.5
LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}
ACTION_FIELDS = ("id", "seq", "n", "action", "note", "state", "result")


def host_only(header):
    """The Host header without its port. A bracketed IPv6 literal keeps its brackets."""
    value = (header or "").strip()
    if value.startswith("["):
        return value.split("]", 1)[0] + "]"
    return value.split(":", 1)[0]


def public_action(record):
    """The delivery contract, with the storage name shortened back to `id`."""
    public = {"id": record.get("action_id")}
    public.update({key: record.get(key) for key in ACTION_FIELDS if key != "id"})
    return public


class Session:
    """Session state, the pub/sub fanout, and the condition awaiters park on."""

    def __init__(self, root, css_path, store=None):
        self.root = root
        self.css_path = css_path
        self.store = store or SessionStore(root)
        self.store.export_json()
        self.cond = threading.Condition()
        self.lock = threading.Lock()
        self.subscribers = []
        self.status = {"phase": "starting", "text": "waiting for the walk to begin"}
        self.stop = threading.Event()
        self.waiting = 0

    @property
    def seq(self):
        return self.store.delivery_state()["seq"]

    @property
    def handled_seq(self):
        return self.store.delivery_state()["handled_seq"]

    def records(self):
        """Every decision on disk that still reads as one.

        seq comes from the records, never from a line count: a single blank line in
        the log would put seq ahead of the highest record, and every /await would
        then answer instantly with a timeout while the agent parked again.

        A half-written trailing line is what ENOSPC or a power loss during the append
        leaves behind, and refusing to parse it stranded the whole session: the server
        would not start, and a live one answered every /await with a 500. One lost
        decision is the smaller harm.
        """
        decisions = self.root / "decisions.jsonl"
        if not decisions.exists():
            return []
        out = []
        for line in decisions.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if isinstance(record, dict):
                    out.append(record)
            except json.JSONDecodeError:
                continue
        return out

    def load(self):
        return rr().load(self.root, self.css_path)

    def fingerprint(self):
        return str(self.store.delivery_state()["render_revision"])

    # ---- pub/sub -------------------------------------------------------

    def snapshot(self):
        # `listening` is the half `status` cannot tell you: status is whatever the agent
        # last said, and an agent that died mid-walk leaves its last word standing.
        delivery = self.store.delivery_state()
        return {
            "session_id": delivery["session_id"],
            "rev": str(delivery["render_revision"]),
            "seq": delivery["seq"],
            "handled_seq": delivery["handled_seq"],
            "head_id": delivery["head_id"],
            "head_kind": delivery["head_kind"],
            "head_state": delivery["head_state"],
            "recovery": delivery["recovery"],
            "status": self.status,
            "listening": self.waiting > 0,
        }

    def subscribe(self, initial=False):
        channel = queue.Queue()
        with self.lock:
            if initial:
                channel.put(self.snapshot())
            self.subscribers.append(channel)
        return channel

    def unsubscribe(self, channel):
        with self.lock:
            if channel in self.subscribers:
                self.subscribers.remove(channel)

    def publish(self):
        with self.lock:
            event = self.snapshot()
            for channel in self.subscribers:
                channel.put(event)

    def set_status(self, status):
        phase = status.get("phase", "working")
        text = status.get("text", "")
        if not isinstance(phase, str) or phase.strip() not in ("working", "parked", "done"):
            raise ValueError("phase must be working, parked, or done")
        if not isinstance(text, str):
            raise ValueError("text must be text")
        self.status = {
            "phase": phase.strip(),
            "text": text.strip()[:MAX_STATUS_TEXT],
        }
        for key in ("beat", "sha"):
            if status.get(key) is not None:
                self.status[key] = status[key]
        self.publish()
        return self.status

    # ---- actions -------------------------------------------------------

    def act(self, n, action, note, action_id=None, session_id=None):
        """Produce one idempotent reviewer action and wake the parked walk."""
        with self.cond:
            record = self.store.produce(
                action_id or str(uuid.uuid4()), n, action, note, session_id
            )
            self.store.export_json()
            self.cond.notify_all()
        name = "decision" if action in ("accept", "drop", "decide") else action
        self.set_status({"phase": "working", "text": f"picking up your {name}"})
        return record

    def ack(self, seq):
        """Acknowledge the applied head and expose the next queued action."""
        with self.cond:
            result = self.store.ack(seq)
            self.store.export_json()
            self.cond.notify_all()
        self.publish()
        return result

    def wait(self, timeout, gone=None):
        """Block until an action newer than the handled cursor lands.

        Both edges of the park are published, because whether anyone is here to take
        the next call is the one thing the page cannot infer from the files. The count
        is kept under `cond` rather than `lock`, which is the documented order.
        """
        with self.cond:
            found = self.store.head()
            if found is not None:
                return found
            self.waiting += 1
            self.publish()
            deadline = time.monotonic() + timeout
            try:
                while True:
                    if gone is not None and gone():
                        return None
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return None
                    self.cond.wait(min(WAIT_SLICE, left))
                    found = self.store.head()
                    if found is not None:
                        return found
            finally:
                self.waiting -= 1
                self.publish()

    def watch(self):
        """Publish changes committed by the session CLI in another process."""
        try:
            delivery = self.store.delivery_state()
            last = tuple(
                delivery[key]
                for key in (
                    "session_id", "render_revision", "seq", "handled_seq", "recovery"
                )
            )
        except (OSError, sqlite3.Error, StoreError):
            last = None
        while not self.stop.wait(WATCH_INTERVAL):
            try:
                delivery = self.store.delivery_state()
                current = tuple(
                    delivery[key]
                    for key in (
                        "session_id", "render_revision", "seq", "handled_seq", "recovery"
                    )
                )
                if last is not None and current != last:
                    with self.cond:
                        self.cond.notify_all()
                    self.publish()
                last = current
            except (OSError, sqlite3.Error, StoreError):
                pass


class Handler(BaseHTTPRequestHandler):
    session = None
    protocol_version = "HTTP/1.1"
    # Without this a half-sent body holds a thread with no deadline at all.
    timeout = SOCKET_TIMEOUT

    def client_gone(self):
        """True once the peer has closed. Readable plus a peek of nothing is EOF."""
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
            return bool(ready) and not self.connection.recv(1, socket.MSG_PEEK)
        except OSError:
            return True

    def log_message(self, *_args):
        pass  # the terminal belongs to the walk, not to an access log

    def forged(self):
        """True when a request reached this port from somewhere other than the walk.

        Host, because any name an attacker controls can be rebound to 127.0.0.1, and
        their page is then same-origin enough to read the diff back out of `/`.
        Origin, because a page on any other origin can POST here with no preflight,
        and `/act` writes into the channel the agent takes its instructions from.
        The page's own origin is always `http://<Host>`, and the walk's curl sends
        no Origin at all.
        """
        host = self.headers.get("Host")
        if host_only(host) not in LOOPBACK:
            return True
        origin = self.headers.get("Origin")
        return origin is not None and origin != f"http://{host.strip()}"

    def send(self, code, body, ctype="application/json"):
        payload = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def stream_events(self):
        """One long-lived response. Heartbeats keep proxies and browsers from closing it."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        channel = self.session.subscribe(initial=True)
        try:
            while True:
                try:
                    event = channel.get(timeout=HEARTBEAT)
                    self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.session.unsubscribe(channel)

    def route(self):
        return self.path.split("?", 1)[0].rstrip("/") or "/"

    def do_GET(self):
        if self.forged():
            return self.send(403, json.dumps({"error": "not a loopback request"}))
        route = self.route()
        try:
            if route == "/events":
                return self.stream_events()
            if route == "/":
                session, beats, css, problems, _ = self.session.load()
                render = rr()
                page = render.SHELL + render.render(
                    session, beats, css, problems, live=True,
                    phase=self.session.status.get("phase"),
                )
                return self.send(200, page, "text/html")
            if route == "/fragment":
                session, beats, _css, problems, _ = self.session.load()
                return self.send(
                    200,
                    rr().body_html(
                        session, beats, problems, True,
                        phase=self.session.status.get("phase"),
                    ),
                    "text/html",
                )
            if route == "/state":
                _s, beats, _c, _p, problems = self.session.load()
                return self.send(
                    200,
                    json.dumps({**self.session.snapshot(), "beats": len(beats), "problems": problems}),
                )
            if route == "/await":
                found = self.session.wait(AWAIT_TIMEOUT, self.client_gone)
                return self.send(
                    200,
                    json.dumps(public_action(found) if found else {"timeout": True}),
                )
            if route == "/favicon.ico":
                return self.send(204, b"", "image/x-icon")
        except (OSError, sqlite3.Error, StoreError, json.JSONDecodeError) as err:
            return self.send(500, json.dumps({"error": str(err)}))
        self.send(404, json.dumps({"error": "no such route"}))

    def do_POST(self):
        if self.forged():
            # The body is still unread, so this connection cannot be reused.
            self.close_connection = True
            return self.send(403, "not a loopback request", "text/plain")
        route = self.route()
        if route not in ("/act", "/status", "/ack"):
            return self.send(404, json.dumps({"error": "no such route"}))
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0:
                # read(-1) drains until EOF, so this held a thread for as long as the
                # client cared to keep the socket open, and then ran the request anyway.
                raise ValueError("Content-Length must not be negative")
            if length > MAX_BODY:
                # Nothing reads the body, so this connection cannot be reused: the
                # next request line would be parsed out of the middle of it.
                self.close_connection = True
                return self.send(413, "body too large", "text/plain")
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("body must be a JSON object")
            if route == "/status":
                return self.send(200, json.dumps(self.session.set_status(payload)))
            if route == "/ack":
                return self.send(200, json.dumps(self.session.ack(payload.get("seq"))))
            action = payload.get("action")
            if action not in ACTIONS:
                raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
            n = payload.get("n")
            if n is not None and (isinstance(n, bool) or not isinstance(n, int)):
                raise ValueError("n must be a positive integer or null")
            note = payload.get("note", "")
            if note is None:
                note = ""
            if not isinstance(note, str):
                raise ValueError("note must be text")
            action_id = payload.get("id")
            if not isinstance(action_id, str) or not action_id.strip():
                raise ValueError("id must be non-empty text")
            session_id = payload.get("session_id")
            if not isinstance(session_id, str) or not session_id.strip():
                raise ValueError("session_id must be non-empty text")
            record = self.session.act(
                n,
                action,
                note.strip(),
                action_id,
                session_id,
            )
        except Conflict as err:
            return self.send(409, str(err), "text/plain")
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as err:
            return self.send(400, str(err), "text/plain")
        except (OSError, sqlite3.Error) as err:
            return self.send(500, str(err), "text/plain")
        self.send(200, json.dumps(public_action(record)))


class Server(ThreadingHTTPServer):
    """The same terminal rule as log_message, applied to the connection layer."""

    daemon_threads = True

    def handle_error(self, request, client_address):
        # A reviewer closing the tab, or a park whose page went away, arrives here as
        # a reset or a timeout, and the default handler prints a full traceback into
        # the terminal the walk is reading. Neither is an error anyone can act on.
        if not isinstance(sys.exc_info()[1], (ConnectionError, socket.timeout)):
            super().handle_error(request, client_address)


class Usage(argparse.ArgumentParser):
    """argparse exits 2 on a usage error, and every script here spends 2 on something
    the caller is meant to keep going after. Usage errors exit 1, as documented."""

    def error(self, message):
        sys.exit(f"serve: {message}")


def already_serving(root, session_id):
    """The URL of a server already answering for this session, or None.

    Two servers on one directory is the cross-process version of the race `act` takes
    a lock to prevent: both accept a racing accept and drop, and both write a record
    numbered seq 1. Neither can see the other's state, so the lock cannot help.

    The proof has to be an answer, not the file and not the pid in it. A SIGKILL
    leaves serve.json behind and a pid can be inherited by something unrelated, and a
    walk that cannot start because of a stale file is the worse failure of the two.
    """
    try:
        url = json.loads((root / "serve.json").read_text(encoding="utf-8"))["url"]
        with urllib.request.urlopen(url + "/state", timeout=1) as answer:
            state = json.loads(answer.read())
            return url if state.get("session_id") == session_id else None
    except (OSError, ValueError, KeyError):
        return None


def main():
    ap = Usage(description="Serve an underwrite session.")
    ap.add_argument("session_dir", help="directory holding session.json and beats/")
    ap.add_argument("--port", type=int, default=0, help="default 0, an ephemeral port")
    ap.add_argument("--css", help="override assets/report.css")
    args = ap.parse_args()

    root = Path(args.session_dir).expanduser()
    if not (root / "session.sqlite3").exists() and not (root / "session.json").exists():
        sys.exit(f"serve: no session database or session.json in {root}")

    try:
        store = SessionStore(root)
        session_id = store.delivery_state()["session_id"]
    except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError) as err:
        sys.exit(f"serve: {err}")

    # The durable identity distinguishes this session from another server that later
    # inherited the stale URL's port. Refusal leaves the live serve.json untouched.
    running = already_serving(root, session_id)
    if running:
        sys.exit(f"serve: this session is already being served at {running}")

    css_path = Path(args.css).expanduser() if args.css else rr().default_css()
    try:
        Handler.session = Session(root, css_path, store)
    except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError) as err:
        sys.exit(f"serve: {err}")
    threading.Thread(target=Handler.session.watch, daemon=True).start()

    try:
        httpd = Server(("127.0.0.1", args.port), Handler)
    except OSError as err:
        sys.exit(f"serve: {err}")

    # Armed before serve.json exists, and the write is inside the try, so there is no
    # instant where the file is on disk and the handler that removes it is not installed.
    # A SIGTERM in that window took the default disposition, and the walk that follows
    # reads the stale file and curls a URL nobody is serving.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        (root / "serve.json").write_text(
            json.dumps({"url": url, "pid": os.getpid()}, indent=2) + "\n", encoding="utf-8"
        )
        print(url, flush=True)
        httpd.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        Handler.session.stop.set()
        httpd.server_close()
        (root / "serve.json").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
