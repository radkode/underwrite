#!/usr/bin/env python3
"""Transactional storage for one underwrite session."""
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


DB_SCHEMA_VERSION = 5
SCHEMA_VERSION = 1
DELIVERY_VERSION = 1
FINDINGS_FILE = "findings.md"
EXECUTION_POLICY_VERSION = 1
EXECUTION_MODES = ("no_exec", "gateway_attested")
AUDIENCE_MODES = ("branch", "review", "report")
BEAT_SLOTS = ("what", "why", "proof", "risk", "prior", "fix")
BEAT_STATES = ("clean", "flag", "unverified", "accepted", "dropped", "decided")
BLOCKED_COMMIT_DELIVERY_REASON = (
    "untrusted PR commit delivery requires a supervised replacement; "
    "do not execute target code"
)
BLOCKED_REPLACEMENT_DELIVERY_SUFFIX = (
    "; start a supervised replacement; do not perform external delivery"
)
MAX_OBJECT_BUNDLE_MEMORY_BYTES = 64 * 1024 * 1024
MAX_IMPLEMENTATION_PROFILE_BYTES = 256 * 1024
MAX_IMPLEMENTATION_REQUEST_BYTES = 384 * 1024
ACTIONS = ("accept", "drop", "decide", "note", "next", "back", "skip")
NAVIGATION = ("next", "back", "skip")
RESOLVE = {"accept": "accepted", "drop": "dropped", "decide": "decided"}
RESOLVABLE = {"accept": ("flag",), "drop": ("flag",), "decide": ("flag", "accepted")}
OPEN_STATES = ("clean", "flag", "unverified")
RESOLUTION_KINDS = ("delivery", "decision")
# Rule 2's budget, in numbers. It binds what a walk writes now and never what is
# already on disk: validate_beat stays the shippability contract for stored beats,
# so tightening here cannot retroactively unship a session or an accepted finding.
BEAT_BUDGET = {
    "claim_words": 20,
    "slot_words": 25,
    "clean_slots": 3,
    "diff_lines": 10,
}
_MISSING = object()
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# Proof names a rerunnable command or path:line, or explicitly records inference.
PROOF_EVIDENCE = re.compile(
    r"`[^`]+`|\b[\w./-]*[A-Za-z][\w./-]*:\d+|^inferred\b"
)
_TARGET_REQUIRED_FIELDS = {
    "version",
    "kind",
    "repo",
    "number",
    "state",
    "merged_at",
    "base_sha",
    "head_sha",
    "head_repo_id",
    "head_repo",
    "head_ref",
    "merge_base_sha",
    "changed_files",
    "diff_sha256",
    "diff_bytes",
}
_TARGET_OPTIONAL_FIELDS = {
    "trusted_context_sha256",
    "trusted_context_bytes",
    "object_bundle_sha256",
    "object_bundle_bytes",
}
_TARGET_FIELDS = _TARGET_REQUIRED_FIELDS | _TARGET_OPTIONAL_FIELDS
_EXECUTION_TARGET_FIELDS = (
    "repo",
    "number",
    "base_sha",
    "head_sha",
    "diff_sha256",
)
_TRUSTED_PROFILE_FIELDS = {
    "version", "keyId", "signerId", "executorId", "job", "sandbox", "exitCode"
}
_IMPLEMENTATION_EVIDENCE_FIELDS = {
    "version", "requestSha256", "keyId", "signerId", "executorId",
    "capabilitySha256", "receiptSha256", "inputTree", "outputTree",
    "outputBundle", "stdout", "stderr", "exitCode",
}
_COMMIT_PLAN_FIELDS = {"version", "commit", "parent", "tree", "outputTree", "branch"}


class StoreError(ValueError):
    pass


class Conflict(StoreError):
    pass


class MigrationError(StoreError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _copy(value):
    return json.loads(_dump(value))


def _legacy_pr_identity(document):
    if not isinstance(document, dict):
        return None
    number = document.get("number")
    repo = document.get("repo")
    if (
        isinstance(number, bool)
        or not isinstance(number, int)
        or number <= 0
        or not isinstance(repo, str)
        or repo.count("/") != 1
        or not all(part.strip() for part in repo.split("/"))
    ):
        return None
    return {"repo": repo, "number": number}


def _non_negative(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StoreError(f"{name} must be a non-negative integer")
    return value


def _positive(value, name):
    _non_negative(value, name)
    if value == 0:
        raise StoreError(f"{name} must be a positive integer")
    return value


def beat_budget_problems(beat):
    """Return the ways a beat about to be written blows rule 2's budget.

    Separate from validate_beat on purpose. This refuses a write while the walk can
    still fix it; validate_beat judges what is already stored, and a beat that was
    legal when it landed stays legal."""
    over = []
    n = beat.get("n", "?")
    claim = beat.get("claim")
    if isinstance(claim, str):
        words = len(claim.split())
        if words > BEAT_BUDGET["claim_words"]:
            over.append(
                f"beat {n}: claim is {words} words, over {BEAT_BUDGET['claim_words']}"
            )
    slots = beat.get("slots")
    if isinstance(slots, dict):
        filled = [key for key in BEAT_SLOTS if slots.get(key)]
        for key in filled:
            words = len(slots[key].split())
            if words > BEAT_BUDGET["slot_words"]:
                over.append(
                    f"beat {n}: {key} is {words} words, over "
                    f"{BEAT_BUDGET['slot_words']}; split the beat or cut it"
                )
        if beat.get("state") == "clean" and len(filled) > BEAT_BUDGET["clean_slots"]:
            over.append(
                f"beat {n}: clean beat fills {len(filled)} slots, over "
                f"{BEAT_BUDGET['clean_slots']} ({', '.join(filled)})"
            )
    quoted = beat.get("diff")
    if isinstance(quoted, list) and len(quoted) > BEAT_BUDGET["diff_lines"]:
        over.append(
            f"beat {n}: {len(quoted)} quoted lines, over {BEAT_BUDGET['diff_lines']}"
        )
    return over


def validate_beat(beat, mode="branch", final=False):
    """Return beat contract violations. Empty means the beat is shippable."""
    problems = []
    n = beat.get("n", "?")
    state = beat.get("state")
    raw_slots = beat.get("slots")
    if raw_slots is None:
        slots = {}
    elif not isinstance(raw_slots, dict):
        problems.append(f"beat {n}: slots must be an object")
        slots = {}
    else:
        slots = raw_slots

    if state not in BEAT_STATES:
        problems.append(
            f"beat {n}: state {state!r} is not one of {', '.join(BEAT_STATES)}"
        )
    for key in slots:
        if key not in BEAT_SLOTS:
            problems.append(f"beat {n}: unknown slot {key!r}")
    resolution_kind = beat.get("resolution_kind", "delivery")
    if resolution_kind not in RESOLUTION_KINDS:
        problems.append(
            f"beat {n}: resolution_kind must be delivery or decision"
        )
    if not slots.get("what"):
        problems.append(f"beat {n}: no what")
    if state in ("clean", "accepted") and not slots.get("proof"):
        problems.append(f"beat {n}: {state} with no proof")
    if state == "accepted" and mode == "report":
        receipt_fields = [
            name for name in ("landed", "branch", "delivery_kind") if name in beat
        ]
        if receipt_fields:
            problems.append(
                f"beat {n}: report outcome cannot have delivery receipt fields: "
                + ", ".join(receipt_fields)
            )
        delivery = beat.get("delivery")
        if (
            isinstance(delivery, dict)
            and delivery.get("state") not in (None, "none")
        ):
            problems.append(
                f"beat {n}: report outcome cannot have "
                f"{delivery.get('state')} delivery"
            )
    elif state == "accepted":
        if final and not beat.get("landed"):
            problems.append(f"beat {n}: accepted, nothing landed")
        if beat.get("landed"):
            expected_delivery = {
                "branch": "commit",
                "review": "review",
            }[mode]
            if beat.get("delivery_kind") != expected_delivery:
                problems.append(
                    f"beat {n}: landed as {beat.get('delivery_kind')!r}, "
                    f"expected {expected_delivery} delivery"
                )
            elif mode == "branch" and not beat.get("branch"):
                problems.append(f"beat {n}: commit delivery has no branch")
            elif mode == "review" and beat.get("branch"):
                problems.append(
                    f"beat {n}: review delivery unexpectedly names a branch"
                )
    if state == "decided" and not beat.get("call"):
        problems.append(f"beat {n}: decided, nothing recorded")
    if beat.get("landed") and state != "accepted":
        problems.append(f"beat {n}: landed {beat['landed']} but state is {state!r}")
    if state == "flag":
        for key in ("risk", "fix"):
            if not slots.get(key):
                problems.append(f"beat {n}: flag with no {key}")
    proof = slots.get("proof")
    if proof and not isinstance(proof, str):
        problems.append(f"beat {n}: proof is {type(proof).__name__}, not text")
    elif proof and not PROOF_EVIDENCE.search(proof):
        problems.append(f"beat {n}: proof names no command or path:line")
    return problems


def _fsync_directory(path):
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _one_line(text):
    """Agent-authored text, flattened so it can never begin a line of the document."""
    return " ".join(str(text).split())


def render_findings(session, beats):
    """The accepted findings as Markdown, derived from the store.

    Report mode has no external delivery, so this file is the only place a finding
    the reviewer included is readable once the session directory's tooling is gone.
    """
    target = session.get("target") or {}
    name = target.get("repo") or "session"
    if target.get("number"):
        name = f"{name}#{target['number']}"
    head = str(target.get("head_sha") or "")[:7]

    out = [f"# Findings: {name}"]
    title = _one_line(session.get("title") or "")
    if title:
        out += ["", title]
    if head:
        out += ["", f"Frozen at `{head}`. Report audience, so nothing posts to GitHub."]
    out += ["", "Derived from `session.sqlite3`. Edits here are overwritten."]

    included = [beat for beat in beats if beat.get("state") == "accepted"]
    plural = "" if len(included) == 1 else "s"
    out += ["", f"{len(included)} finding{plural} included."]

    for beat in included:
        out += ["", "", f"## Beat {beat.get('n')}: {_one_line(beat.get('claim') or '')}"]
        bullets = []
        where = _one_line(beat.get("where") or "")
        if where:
            bullets.append(f"- **Where** `{where}`")
        tier = _one_line(beat.get("tier") or "")
        if tier:
            bullets.append(f"- **Tier** {tier}")
        slots = beat.get("slots") or {}
        for slot in BEAT_SLOTS:
            value = _one_line(slots.get(slot) or "")
            if value:
                bullets.append(f"- **{slot.upper()}** {value}")
        if bullets:
            out += [""] + bullets
        call = _one_line(beat.get("call") or "")
        if call:
            out += ["", f"**Your call.** {call}"]

    return "\n".join(out) + "\n"


def _atomic_write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        _fsync_directory(path.parent)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _stage_copy(source, directory, label):
    source = Path(source)
    fd, name = tempfile.mkstemp(prefix=f".{label}.", suffix=".tmp", dir=str(directory))
    digest, size = hashlib.sha256(), 0
    try:
        with source.open("rb") as incoming, os.fdopen(fd, "wb") as outgoing:
            while True:
                chunk = incoming.read(1024 * 1024)
                if not chunk:
                    break
                outgoing.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        return Path(name), digest.hexdigest(), size
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass
        raise


def _file_identity(path):
    digest, size = hashlib.sha256(), 0
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _bounded_file_identity(path, expected_size, label):
    if not hasattr(os, "O_NOFOLLOW"):
        raise Conflict("target verification requires no-follow file opens")
    try:
        descriptor = os.open(
            os.fsencode(path),
            os.O_RDONLY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOCTTY", 0),
        )
    except FileNotFoundError:
        raise
    except OSError as error:
        raise Conflict(f"{label} cannot be opened safely") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size != expected_size
        ):
            raise Conflict(f"{label} is not one exact regular file")
        digest = hashlib.sha256()
        remaining = expected_size
        while remaining:
            block = os.read(descriptor, min(remaining, 1024 * 1024))
            if not block:
                raise Conflict(f"{label} became shorter while verified")
            digest.update(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise Conflict(f"{label} became longer while verified")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if identity(before) != identity(after):
        raise Conflict(f"{label} changed while verified")
    return digest.hexdigest(), expected_size


def _bounded_file_bytes(path, expected_size, label):
    if not hasattr(os, "O_NOFOLLOW"):
        raise Conflict("target reads require no-follow file opens")
    try:
        descriptor = os.open(
            os.fsencode(path),
            os.O_RDONLY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOCTTY", 0),
        )
    except FileNotFoundError:
        raise
    except OSError as error:
        raise Conflict(f"{label} cannot be opened safely") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size != expected_size
        ):
            raise Conflict(f"{label} is not one exact regular file")
        data = bytearray()
        while len(data) < expected_size:
            block = os.read(
                descriptor,
                min(expected_size - len(data), 1024 * 1024),
            )
            if not block:
                raise Conflict(f"{label} became shorter while read")
            data.extend(block)
        if os.read(descriptor, 1):
            raise Conflict(f"{label} became longer while read")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if identity(before) != identity(after):
        raise Conflict(f"{label} changed while read")
    return bytes(data)


class SessionStore:
    """The sole mutable authority for one session directory."""

    def __init__(self, root, handled_override=None):
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "session.sqlite3"
        self.lock_path = self.root / ".session.lock"
        with self._session_lock():
            if not self.path.exists():
                self._migrate_legacy(handled_override)
            elif handled_override is not None:
                raise StoreError("handled_override is only valid during legacy migration")
            self._upgrade_schema()
            self._verify_version()
            _fsync_directory(self.root)

    # ---- connection and schema ---------------------------------------

    def _connect_to(self, path):
        db = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout = 5000")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > DB_SCHEMA_VERSION:
            db.close()
            raise StoreError(
                f"session database version {version} is newer than supported "
                f"{DB_SCHEMA_VERSION}"
            )
        db.execute("PRAGMA foreign_keys = ON")
        mode = db.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
        if mode.lower() != "delete":
            db.close()
            raise StoreError(f"session database journal mode is {mode}, not delete")
        db.execute("PRAGMA synchronous = EXTRA")
        return db

    def _connect(self):
        return self._connect_to(self.path)

    @contextlib.contextmanager
    def _session_lock(self):
        with self.lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextlib.contextmanager
    def _write(self):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    @contextlib.contextmanager
    def _first_work_write(self):
        with self._session_lock():
            with self._write() as db:
                yield db

    @contextlib.contextmanager
    def _read(self):
        db = self._connect()
        try:
            db.execute("BEGIN")
            yield db
            db.execute("COMMIT")
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def _create_schema(self, db):
        statements = (
            """CREATE TABLE session (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                session_id TEXT NOT NULL UNIQUE,
                format_version INTEGER NOT NULL,
                render_revision INTEGER NOT NULL DEFAULT 0 CHECK (render_revision >= 0),
                body_json TEXT NOT NULL
            )""",
            """CREATE TABLE beats (
                n INTEGER PRIMARY KEY CHECK (n > 0),
                revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
                body_json TEXT NOT NULL,
                delivery_state TEXT NOT NULL DEFAULT 'none'
                    CHECK (delivery_state IN ('none', 'pending', 'failed', 'landed')),
                delivery_json TEXT
            )""",
            """CREATE TABLE actions (
                seq INTEGER PRIMARY KEY CHECK (seq > 0),
                action_id TEXT NOT NULL UNIQUE,
                beat_n INTEGER REFERENCES beats(n),
                kind TEXT NOT NULL
                    CHECK (kind IN ('accept', 'drop', 'decide', 'note', 'next', 'back', 'skip')),
                note TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL
                    CHECK (state IN ('produced', 'applied', 'acked', 'abandoned')),
                result_json TEXT,
                evidence TEXT NOT NULL DEFAULT '',
                produced_at TEXT NOT NULL,
                applied_at TEXT,
                acked_at TEXT,
                abandoned_at TEXT,
                abandoned_by TEXT,
                abandoned_reason TEXT,
                CHECK (
                    (state = 'produced' AND result_json IS NULL) OR
                    (state IN ('applied', 'acked') AND result_json IS NOT NULL) OR
                    state = 'abandoned'
                )
            )""",
            """CREATE TABLE implementation_links (
                link_id TEXT PRIMARY KEY,
                source_action_seq INTEGER NOT NULL UNIQUE REFERENCES actions(seq),
                source_action_id TEXT NOT NULL,
                source_beat INTEGER NOT NULL REFERENCES beats(n),
                source_beat_revision INTEGER NOT NULL CHECK (source_beat_revision >= 0),
                source_beat_json TEXT NOT NULL,
                source_beat_sha256 TEXT NOT NULL,
                target_sha256 TEXT NOT NULL,
                actor TEXT NOT NULL,
                approval TEXT NOT NULL,
                child_path TEXT NOT NULL UNIQUE,
                branch TEXT NOT NULL,
                child_session_id TEXT UNIQUE,
                state TEXT NOT NULL CHECK (state IN ('reserved', 'ready')),
                created_at TEXT NOT NULL,
                ready_at TEXT
            )""",
            """CREATE TABLE implementation_attempts (
                action_seq INTEGER NOT NULL REFERENCES actions(seq),
                attempt INTEGER NOT NULL CHECK (attempt > 0),
                challenge TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL
                    CHECK (state IN ('reserved', 'verified', 'prepared', 'landed', 'failed')),
                trusted_profile_json TEXT NOT NULL,
                request_json TEXT NOT NULL,
                request_sha256 TEXT NOT NULL,
                capability BLOB,
                receipt BLOB,
                evidence_json TEXT,
                commit_plan_json TEXT,
                failure TEXT,
                created_at TEXT NOT NULL,
                verified_at TEXT,
                prepared_at TEXT,
                landed_at TEXT,
                failed_at TEXT,
                PRIMARY KEY (action_seq, attempt)
            )""",
        )
        for statement in statements:
            db.execute(statement)
        db.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")

    def _upgrade_schema(self):
        db = self._connect()
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == DB_SCHEMA_VERSION:
                return
            if version not in (1, 2, 3, 4):
                raise StoreError(f"unsupported session database version {version}")
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT format_version FROM session WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise StoreError("session database has no session row")
            if row["format_version"] != SCHEMA_VERSION:
                raise StoreError(
                    f"unsupported session format version {row['format_version']}"
                )
            if version == 1:
                columns = {
                    row["name"] for row in db.execute("PRAGMA table_info(session)")
                }
                if "session_id" not in columns:
                    db.execute("ALTER TABLE session ADD COLUMN session_id TEXT")
                row = db.execute(
                    "SELECT session_id FROM session WHERE singleton = 1"
                ).fetchone()
                if not isinstance(row["session_id"], str) or not row["session_id"].strip():
                    db.execute(
                        "UPDATE session SET session_id = ? WHERE singleton = 1",
                        (str(uuid.uuid4()),),
                    )
                db.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS session_identity "
                    "ON session(session_id)"
                )
            if version in (1, 2):
                self._restore_failed_fix_intents(db)
            if version in (1, 2, 3):
                self._default_legacy_execution_policy(db)
            self._create_implementation_tables(db)
            db.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")
            db.execute("COMMIT")
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def _create_implementation_tables(self, db):
        db.execute("""CREATE TABLE IF NOT EXISTS implementation_links (
            link_id TEXT PRIMARY KEY,
            source_action_seq INTEGER NOT NULL UNIQUE REFERENCES actions(seq),
            source_action_id TEXT NOT NULL,
            source_beat INTEGER NOT NULL REFERENCES beats(n),
            source_beat_revision INTEGER NOT NULL CHECK (source_beat_revision >= 0),
            source_beat_json TEXT NOT NULL,
            source_beat_sha256 TEXT NOT NULL,
            target_sha256 TEXT NOT NULL,
            actor TEXT NOT NULL,
            approval TEXT NOT NULL,
            child_path TEXT NOT NULL UNIQUE,
            branch TEXT NOT NULL,
            child_session_id TEXT UNIQUE,
            state TEXT NOT NULL CHECK (state IN ('reserved', 'ready')),
            created_at TEXT NOT NULL,
            ready_at TEXT
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS implementation_attempts (
            action_seq INTEGER NOT NULL REFERENCES actions(seq),
            attempt INTEGER NOT NULL CHECK (attempt > 0),
            challenge TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL
                CHECK (state IN ('reserved', 'verified', 'prepared', 'landed', 'failed')),
            trusted_profile_json TEXT NOT NULL,
            request_json TEXT NOT NULL,
            request_sha256 TEXT NOT NULL,
            capability BLOB,
            receipt BLOB,
            evidence_json TEXT,
            commit_plan_json TEXT,
            failure TEXT,
            created_at TEXT NOT NULL,
            verified_at TEXT,
            prepared_at TEXT,
            landed_at TEXT,
            failed_at TEXT,
            PRIMARY KEY (action_seq, attempt)
        )""")

    def _default_legacy_execution_policy(self, db):
        row = self._session_row(db)
        body = json.loads(row["body_json"])
        had_legacy_pr = "legacy_pr" in body
        had_execution_policy = "execution_policy" in body
        body.pop("legacy_pr", None)
        body.pop("execution_policy", None)
        target = body.get("target")
        if target is None:
            identity = _legacy_pr_identity(body)
            if identity is not None:
                body["legacy_pr"] = identity
            self._session_document(body)
            if had_legacy_pr or had_execution_policy or identity is not None:
                db.execute(
                    "UPDATE session SET body_json = ?, "
                    "render_revision = render_revision + 1 WHERE singleton = 1",
                    (_dump(body),),
                )
            self._migrate_application_sessions(db)
            return
        # Versions before v4 had no trusted execution-policy gateway. Never honor a
        # similarly named field that arrived through their permissive session document.
        body["execution_policy"] = self._execution_policy(target, "no_exec")
        self._session_document(body)
        db.execute(
            "UPDATE session SET body_json = ?, render_revision = render_revision + 1 "
            "WHERE singleton = 1",
            (_dump(body),),
        )
        self._migrate_application_sessions(
            db, target=target, policy=body["execution_policy"]
        )

    def _migrate_application_sessions(self, db, target=None, policy=None):
        rows = db.execute(
            "SELECT seq, result_json FROM actions WHERE result_json IS NOT NULL"
        ).fetchall()
        for row in rows:
            application = json.loads(row["result_json"])
            session = application.get("session")
            if not isinstance(session, dict):
                continue
            session.pop("legacy_pr", None)
            session.pop("execution_policy", None)
            if target is not None:
                if session.get("target") != target:
                    continue
                session["execution_policy"] = _copy(policy)
            else:
                if session.get("target") is not None:
                    continue
                identity = _legacy_pr_identity(session)
                if identity is not None:
                    session["legacy_pr"] = identity
            _version, application["session"] = self._session_document(session)
            db.execute(
                "UPDATE actions SET result_json = ? WHERE seq = ?",
                (_dump(application), row["seq"]),
            )

    def _restore_failed_fix_intents(self, db):
        changed = False
        rows = db.execute(
            "SELECT n, body_json, delivery_json FROM beats "
            "WHERE delivery_state = 'failed'"
        ).fetchall()
        for row in rows:
            delivery = json.loads(row["delivery_json"]) if row["delivery_json"] else {}
            action = db.execute(
                "SELECT beat_n, kind, result_json FROM actions WHERE seq = ?",
                (delivery.get("cause_seq"),),
            ).fetchone()
            if (
                action is None
                or action["kind"] != "accept"
                or action["beat_n"] != row["n"]
                or not action["result_json"]
            ):
                continue
            application = json.loads(action["result_json"])
            approved = next(
                (
                    beat
                    for beat in application.get("beats", [])
                    if isinstance(beat, dict) and beat.get("n") == row["n"]
                ),
                None,
            )
            approved_slots = approved.get("slots") if isinstance(approved, dict) else None
            beat = json.loads(row["body_json"])
            slots = beat.get("slots")
            if not isinstance(slots, dict):
                continue
            if isinstance(approved_slots, dict) and "fix" in approved_slots:
                if slots.get("fix") == approved_slots["fix"]:
                    continue
                slots["fix"] = _copy(approved_slots["fix"])
            elif slots.get("fix") == delivery.get("owed"):
                slots.pop("fix")
                if not slots and not isinstance(approved_slots, dict):
                    beat.pop("slots", None)
            else:
                continue
            db.execute(
                "UPDATE beats SET revision = revision + 1, body_json = ? WHERE n = ?",
                (_dump(beat), row["n"]),
            )
            changed = True
        if changed:
            db.execute(
                "UPDATE session SET render_revision = render_revision + 1 "
                "WHERE singleton = 1"
            )

    def _verify_version(self):
        db = self._connect()
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > DB_SCHEMA_VERSION:
                raise StoreError(
                    f"session database version {version} is newer than supported "
                    f"{DB_SCHEMA_VERSION}"
                )
            if version != DB_SCHEMA_VERSION:
                raise StoreError(f"unsupported session database version {version}")
            row = db.execute(
                "SELECT session_id, format_version, body_json FROM session "
                "WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise StoreError("session database has no session row")
            if not isinstance(row["session_id"], str) or not row["session_id"].strip():
                raise StoreError("session database has no session identity")
            if row["format_version"] != SCHEMA_VERSION:
                raise StoreError(
                    f"unsupported session format version {row['format_version']}"
                )
            self._session_document(json.loads(row["body_json"]))
        finally:
            db.close()

    # ---- legacy import ------------------------------------------------

    def _migrate_legacy(self, handled_override):
        if handled_override is not None:
            _non_negative(handled_override, "handled_override")
        session, beats, actions = self._legacy_data(handled_override)
        fd, temp_name = tempfile.mkstemp(
            prefix=".session.sqlite3.", suffix=".tmp", dir=str(self.root)
        )
        os.close(fd)
        os.unlink(temp_name)
        temp = Path(temp_name)
        try:
            db = self._connect_to(temp)
            try:
                db.execute("BEGIN IMMEDIATE")
                self._create_schema(db)
                legacy_session = _copy(session)
                exported_legacy_pr = legacy_session.pop("legacy_pr", None)
                legacy_session.pop("execution_policy", None)
                if "target" not in legacy_session:
                    identity = _legacy_pr_identity(legacy_session)
                    if (
                        isinstance(exported_legacy_pr, dict)
                        and _legacy_pr_identity(exported_legacy_pr)
                        == exported_legacy_pr
                    ):
                        legacy_session["legacy_pr"] = _copy(exported_legacy_pr)
                    elif identity is not None:
                        legacy_session["legacy_pr"] = identity
                version, body = self._session_document(legacy_session)
                if "target" in body:
                    body["execution_policy"] = self._execution_policy(
                        body["target"], "no_exec"
                    )
                db.execute(
                    "INSERT INTO session "
                    "(singleton, session_id, format_version, render_revision, body_json) "
                    "VALUES (1, ?, ?, 0, ?)",
                    (str(uuid.uuid4()), version, _dump(body)),
                )
                accept_seqs = {}
                for action in actions:
                    if action["kind"] == "accept":
                        beat_n = action["beat_n"]
                        if beat_n in accept_seqs:
                            raise MigrationError(
                                f"multiple legacy accepts name beat {beat_n}"
                            )
                        accept_seqs[beat_n] = action["seq"]
                delivery_kind = self._expected_delivery_kind(session)
                report = self._audience_mode(session) == "report"
                for beat in beats:
                    accept_seq = accept_seqs.get(beat["n"])
                    if (
                        report
                        and beat.get("state") == "accepted"
                        and accept_seq is None
                    ):
                        raise MigrationError(
                            f"accepted legacy beat {beat['n']} has no accept action"
                        )
                    if report and beat.get("state") == "accepted":
                        problems = validate_beat(beat, "report", final=True)
                        if problems:
                            raise MigrationError(
                                f"accepted report beat {beat['n']} is not shippable: "
                                + "; ".join(problems)
                            )
                    if report and any(
                        field in beat
                        for field in ("landed", "branch", "delivery_kind")
                    ):
                        raise MigrationError(
                            f"report legacy beat {beat['n']} has external delivery fields"
                        )
                    state, delivery = self._delivery_from_document(
                        beat,
                        cause_seq=accept_seq,
                        kind=delivery_kind,
                        report=report,
                    )
                    if state == "pending" and delivery["cause_seq"] is None:
                        raise MigrationError(
                            f"accepted legacy beat {beat['n']} has no accept action"
                        )
                    if state == "pending" and delivery["kind"] is None:
                        raise MigrationError(
                            f"accepted legacy beat {beat['n']} has no delivery audience"
                        )
                    if state == "landed" and delivery["kind"] is None:
                        raise MigrationError(
                            f"landed legacy beat {beat['n']} has no delivery audience"
                        )
                    if state == "landed":
                        if delivery["kind"] == "commit" and not delivery.get("branch"):
                            raise MigrationError(
                                f"landed legacy beat {beat['n']} has no branch"
                            )
                        if delivery["kind"] == "review" and delivery.get("branch"):
                            raise MigrationError(
                                f"review legacy beat {beat['n']} unexpectedly has a branch"
                            )
                        beat["delivery_kind"] = delivery["kind"]
                    db.execute(
                        "INSERT INTO beats VALUES (?, 0, ?, ?, ?)",
                        (beat["n"], _dump(beat), state, self._optional_dump(delivery)),
                    )
                for action in actions:
                    self._insert_action(db, **action)
                db.execute("COMMIT")
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
            finally:
                db.close()
            os.replace(str(temp), str(self.path))
            _fsync_directory(self.root)
        finally:
            for leftover in (temp, Path(str(temp) + "-journal")):
                try:
                    leftover.unlink()
                except FileNotFoundError:
                    pass

    def _legacy_data(self, handled_override):
        session_path = self.root / "session.json"
        try:
            session = (
                json.loads(session_path.read_text(encoding="utf-8"))
                if session_path.exists()
                else {}
            )
        except UnicodeError as error:
            raise MigrationError("session.json is not valid UTF-8") from error
        if not isinstance(session, dict):
            raise MigrationError("session.json must contain an object")
        if "linked_implementation" in session:
            raise MigrationError(
                "linked implementation sessions require their authoritative database"
            )

        beats = []
        seen_beats = set()
        for path in sorted((self.root / "beats").glob("*.json")):
            try:
                beat = json.loads(path.read_text(encoding="utf-8"))
            except UnicodeError as error:
                raise MigrationError(f"{path.name} is not valid UTF-8") from error
            beat = self._beat_document(beat)
            if beat["n"] in seen_beats:
                raise MigrationError(f"duplicate legacy beat {beat['n']}")
            seen_beats.add(beat["n"])
            beats.append(beat)

        records = []
        decisions = self.root / "decisions.jsonl"
        if decisions.exists():
            try:
                decision_lines = decisions.read_text(encoding="utf-8").splitlines()
            except UnicodeError as error:
                raise MigrationError("decisions.jsonl is not valid UTF-8") from error
            for line_number, line in enumerate(
                decision_lines, 1
            ):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as error:
                    raise MigrationError(
                        f"decisions.jsonl line {line_number} is malformed: {error.msg}"
                    ) from error
                if not isinstance(record, dict):
                    raise MigrationError(
                        f"decisions.jsonl line {line_number} must be an object"
                    )
                records.append(record)

        seqs = set()
        versioned = False
        for record in records:
            seq = _positive(record.get("seq"), "legacy action seq")
            if seq in seqs:
                raise MigrationError(f"duplicate legacy action seq {seq}")
            seqs.add(seq)
            version = record.get("delivery_version", _MISSING)
            if version is not _MISSING:
                versioned = True
                if type(version) is not int or version != DELIVERY_VERSION:
                    raise MigrationError(f"unsupported legacy delivery version {version!r}")

        maximum = max(seqs, default=0)
        if seqs != set(range(1, maximum + 1)):
            raise MigrationError("legacy action sequence is not contiguous from 1")
        handled = self._legacy_handled(maximum, versioned, handled_override)
        by_n = {beat["n"]: beat for beat in beats}
        actions = []
        for record in sorted(records, key=lambda item: item["seq"]):
            seq = record["seq"]
            kind = record.get("action")
            if kind not in ACTIONS:
                raise MigrationError(f"unsupported legacy action {kind!r}")
            n = record.get("n")
            if n is not None:
                n = _positive(n, "legacy action beat")
            note = record.get("note") or ""
            if not isinstance(note, str):
                raise MigrationError("legacy action note must be text")
            state, application = "produced", None
            if seq <= handled:
                state = "acked"
                application = self._application(
                    {"kind": "legacy", "handled": True}, None, ()
                )
            elif kind not in NAVIGATION:
                application = self._legacy_application(kind, n, note, by_n)
                if application is not None:
                    state = "applied"
            timestamp = _now()
            actions.append({
                "seq": seq,
                "action_id": record.get("action_id") or f"legacy:{seq}",
                "beat_n": n,
                "kind": kind,
                "note": note,
                "state": state,
                "application": application,
                "evidence": "legacy JSON import",
                "produced_at": timestamp,
                "applied_at": timestamp if state in ("applied", "acked") else None,
                "acked_at": timestamp if state == "acked" else None,
            })
        return session, beats, actions

    def _legacy_handled(self, maximum, versioned, override):
        if override is not None:
            if override > maximum:
                raise MigrationError(
                    f"handled_override {override} is ahead of produced seq {maximum}"
                )
            return override
        path = self.root / "ack.json"
        if path.exists():
            try:
                ack = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise MigrationError(
                    "ack.json is unreadable; rerun init with --handled-seq after inspection"
                ) from error
            if (
                not isinstance(ack, dict)
                or type(ack.get("version")) is not int
                or ack.get("version") != DELIVERY_VERSION
            ):
                raise MigrationError(
                    "ack.json has an unsupported shape; rerun init with --handled-seq "
                    "after inspection"
                )
            handled = ack.get("handled_seq")
            try:
                _non_negative(handled, "ack.json handled_seq")
            except StoreError as error:
                raise MigrationError(
                    "ack.json has an invalid cursor; rerun init with --handled-seq "
                    "after inspection"
                ) from error
            if handled > maximum:
                raise MigrationError(
                    "ack.json is ahead of the action log; rerun init with --handled-seq "
                    "after inspection"
                )
            return handled
        if versioned:
            raise MigrationError(
                "ack.json is missing from a cursor-aware session; rerun init with "
                "--handled-seq after inspection"
            )
        return maximum

    def _legacy_application(self, kind, n, note, beats):
        beat = beats.get(n)
        if beat is None:
            return None
        expected = RESOLVE.get(kind)
        if expected is not None and beat.get("state") != expected:
            return None
        if kind == "note" and note and beat.get("call") != note:
            return None
        if note and kind in RESOLVE and beat.get("call") != note:
            return None
        result = {
            "kind": "beat",
            "n": n,
            "state": beat.get("state"),
            "call": beat.get("call", ""),
        }
        return self._application(result, None, (beat,))

    # ---- documents ----------------------------------------------------

    def _session_document(self, document):
        if not isinstance(document, dict):
            raise StoreError("session must be an object")
        body = _copy(document)
        version = body.pop("schema_version", SCHEMA_VERSION)
        if type(version) is not int or version != SCHEMA_VERSION:
            raise StoreError(f"session schema_version must be {SCHEMA_VERSION}")
        audience = body.get("audience")
        if audience is not None:
            if not isinstance(audience, dict):
                raise StoreError("session audience must be an object")
            if audience.get("mode") not in AUDIENCE_MODES:
                raise StoreError(
                    "session audience mode must be branch, review, or report"
                )
        delivery_branch = body.get("delivery_branch")
        if delivery_branch is not None and (
            not isinstance(delivery_branch, str)
            or not delivery_branch.strip()
            or delivery_branch != delivery_branch.strip()
            or any(ord(character) < 32 for character in delivery_branch)
        ):
            raise StoreError("session delivery_branch must be a valid non-empty name")
        if "target" in body:
            body["target"] = self._target_document(body["target"])
        legacy_pr = body.get("legacy_pr")
        if legacy_pr is not None:
            if "target" in body:
                raise StoreError("session legacy_pr cannot accompany a frozen target")
            if not isinstance(legacy_pr, dict) or set(legacy_pr) != {"repo", "number"}:
                raise StoreError("session legacy_pr has an unsupported shape")
            if _legacy_pr_identity(legacy_pr) != legacy_pr:
                raise StoreError("session legacy_pr identity is invalid")
        audience_mode = audience.get("mode") if isinstance(audience, dict) else None
        if audience_mode == "report" and "target" not in body:
            raise StoreError("report audience requires a frozen PR target")
        if (
            audience_mode == "review"
            and "target" not in body
            and legacy_pr is None
        ):
            raise StoreError(
                "review audience requires a frozen PR target or legacy PR marker"
            )
        if audience_mode == "report" and body.get("lands") not in (None, []):
            raise StoreError("report audience cannot have lands")
        linked = body.get("linked_implementation")
        if linked is not None:
            linked = self._linked_implementation_document(linked)
            body["linked_implementation"] = linked
            if audience_mode != "branch":
                raise StoreError("linked implementation requires branch audience")
            if not delivery_branch:
                raise StoreError("linked implementation requires a delivery branch")
        if "execution_policy" in body:
            if "target" not in body:
                raise StoreError("session execution_policy requires a frozen target")
            body["execution_policy"] = self._execution_policy_document(
                body["execution_policy"], body["target"], linked
            )
        if linked is not None and body.get("execution_policy", {}).get("mode") != "gateway_attested":
            raise StoreError("linked implementation requires gateway_attested execution")
        return version, body

    def _target_document(self, document):
        if not isinstance(document, dict):
            raise StoreError("session target must be an object")
        target = _copy(document)
        missing = sorted(_TARGET_REQUIRED_FIELDS - set(target))
        unknown = sorted(set(target) - _TARGET_FIELDS)
        if missing:
            raise StoreError(f"session target is missing {', '.join(missing)}")
        if unknown:
            raise StoreError(f"session target has unknown field {', '.join(unknown)}")
        if type(target["version"]) is not int or target["version"] != 1:
            raise StoreError("session target version must be 1")
        if target["kind"] != "github_pr":
            raise StoreError("session target kind must be github_pr")
        repo = target["repo"]
        if (
            not isinstance(repo, str)
            or repo.count("/") != 1
            or not all(part.strip() for part in repo.split("/"))
        ):
            raise StoreError("session target repo must be owner/name")
        _positive(target["number"], "session target number")
        if target["state"] not in ("open", "closed"):
            raise StoreError("session target state must be open or closed")
        if target["merged_at"] is not None and (
            not isinstance(target["merged_at"], str) or not target["merged_at"].strip()
        ):
            raise StoreError("session target merged_at must be null or non-empty text")
        for name in ("base_sha", "head_sha", "merge_base_sha"):
            value = target[name]
            if not isinstance(value, str) or not _FULL_SHA.fullmatch(value):
                raise StoreError(f"session target {name} must be a full lowercase SHA")
        head_repo = target["head_repo"]
        head_repo_id = target["head_repo_id"]
        if head_repo is None:
            if head_repo_id is not None:
                raise StoreError(
                    "session target head_repo_id must be null when head_repo is null"
                )
        else:
            if (
                not isinstance(head_repo, str)
                or head_repo.count("/") != 1
                or not all(part.strip() for part in head_repo.split("/"))
            ):
                raise StoreError("session target head_repo must be null or owner/name")
            _positive(head_repo_id, "session target head_repo_id")
        if not isinstance(target["head_ref"], str) or not target["head_ref"].strip():
            raise StoreError("session target head_ref must be non-empty text")
        _non_negative(target["changed_files"], "session target changed_files")
        if (
            not isinstance(target["diff_sha256"], str)
            or not _SHA256.fullmatch(target["diff_sha256"])
        ):
            raise StoreError("session target diff_sha256 must be a SHA-256 digest")
        _non_negative(target["diff_bytes"], "session target diff_bytes")
        for digest_name, size_name, label in (
            (
                "trusted_context_sha256",
                "trusted_context_bytes",
                "trusted context",
            ),
            ("object_bundle_sha256", "object_bundle_bytes", "object bundle"),
        ):
            present = [name for name in (digest_name, size_name) if name in target]
            if present and len(present) != 2:
                raise StoreError(
                    f"session target {label} digest and size must appear together"
                )
            if not present:
                continue
            if (
                not isinstance(target[digest_name], str)
                or not _SHA256.fullmatch(target[digest_name])
            ):
                raise StoreError(
                    f"session target {digest_name} must be a SHA-256 digest"
                )
            _non_negative(
                target[size_name],
                f"session target {size_name}",
            )
        return target

    def _execution_target(self, target):
        bound = {name: target[name] for name in _EXECUTION_TARGET_FIELDS}
        if "trusted_context_sha256" in target:
            bound["trusted_context_sha256"] = target["trusted_context_sha256"]
        if "object_bundle_sha256" in target:
            bound["object_bundle_sha256"] = target["object_bundle_sha256"]
        return bound

    def _execution_policy(self, target, mode, link_id=None):
        if mode not in EXECUTION_MODES:
            raise StoreError(
                "execution mode must be one of " + ", ".join(EXECUTION_MODES)
            )
        policy = {
            "version": EXECUTION_POLICY_VERSION,
            "trust": "untrusted",
            "mode": mode,
            "target": self._execution_target(target),
        }
        if mode == "gateway_attested":
            if not isinstance(link_id, str) or not _SHA256.fullmatch(link_id):
                raise StoreError(
                    "gateway_attested execution requires linked implementation provenance"
                )
            policy["link_id"] = link_id
        elif link_id is not None:
            raise StoreError("no_exec execution cannot name an implementation link")
        return policy

    def _execution_policy_document(self, document, target, linked=None):
        if not isinstance(document, dict):
            raise StoreError("session execution_policy must be an object")
        mode = document.get("mode")
        link_id = linked.get("link_id") if isinstance(linked, dict) else None
        expected = self._execution_policy(target, mode, link_id)
        if document != expected:
            raise StoreError(
                "session execution_policy does not match its frozen target"
            )
        return expected

    def _linked_implementation_document(self, document):
        fields = {
            "version", "link_id", "source_session_id", "source_action_seq",
            "source_action_id", "source_beat", "source_beat_revision",
            "source_beat_sha256", "target_sha256", "actor", "approval",
        }
        if not isinstance(document, dict) or set(document) != fields:
            raise StoreError("session linked_implementation has an unsupported shape")
        linked = _copy(document)
        if type(linked["version"]) is not int or linked["version"] != 1:
            raise StoreError("session linked_implementation version must be 1")
        for name in ("link_id", "source_beat_sha256", "target_sha256"):
            if not isinstance(linked[name], str) or not _SHA256.fullmatch(linked[name]):
                raise StoreError(f"session linked_implementation {name} is invalid")
        for name in ("source_action_seq", "source_beat"):
            _positive(linked[name], f"session linked_implementation {name}")
        _non_negative(
            linked["source_beat_revision"],
            "session linked_implementation source_beat_revision",
        )
        for name in ("source_session_id", "source_action_id", "actor", "approval"):
            value = linked[name]
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise StoreError(f"session linked_implementation {name} must be text")
        return linked

    def _trusted_context_document(self, data, target):
        try:
            context = json.loads(data.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise StoreError("trusted context is not valid UTF-8 JSON") from error
        if not isinstance(context, dict):
            raise StoreError("trusted context must contain an object")
        if set(context) != {"version", "base_sha", "files"}:
            raise StoreError("trusted context has an unsupported shape")
        if type(context["version"]) is not int or context["version"] != 1:
            raise StoreError("trusted context version must be 1")
        if context["base_sha"] != target["base_sha"]:
            raise StoreError("trusted context base_sha does not match the frozen target")
        files = context["files"]
        if not isinstance(files, list):
            raise StoreError("trusted context files must be an array")
        paths = []
        for index, entry in enumerate(files):
            if not isinstance(entry, dict) or set(entry) != {
                "path",
                "mode",
                "blob_sha",
                "content",
            }:
                raise StoreError(
                    f"trusted context file {index} has an unsupported shape"
                )
            path = entry["path"]
            parts = path.split("/") if isinstance(path, str) else []
            if (
                not isinstance(path, str)
                or not path
                or path.startswith("/")
                or any(part in ("", ".", "..") for part in parts)
                or any(ord(character) < 32 for character in path)
            ):
                raise StoreError(
                    f"trusted context file {index} path must be repo-relative text"
                )
            if not isinstance(entry["mode"], str) or not re.fullmatch(
                r"[0-7]{6}", entry["mode"]
            ):
                raise StoreError(f"trusted context file {index} mode is invalid")
            if entry["mode"] not in ("100644", "100755"):
                raise StoreError(
                    f"trusted context file {index} must be a regular file"
                )
            if not isinstance(entry["blob_sha"], str) or not _FULL_SHA.fullmatch(
                entry["blob_sha"]
            ):
                raise StoreError(f"trusted context file {index} blob_sha is invalid")
            if not isinstance(entry["content"], str):
                raise StoreError(f"trusted context file {index} content must be text")
            content = entry["content"].encode("utf-8")
            framed = b"blob " + str(len(content)).encode("ascii") + b"\0" + content
            if hashlib.sha1(framed).hexdigest() != entry["blob_sha"]:
                raise StoreError(
                    f"trusted context file {index} content does not match blob_sha"
                )
            paths.append(path)
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise StoreError("trusted context files must have unique sorted paths")
        canonical = (
            json.dumps(
                context,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        if data != canonical:
            raise StoreError("trusted context JSON is not canonical")
        return context

    def _beat_document(self, document):
        if not isinstance(document, dict):
            raise StoreError("beat must be an object")
        beat = _copy(document)
        _positive(beat.get("n"), "beat n")
        if (
            "resolution_kind" in beat
            and beat["resolution_kind"] not in RESOLUTION_KINDS
        ):
            raise StoreError(
                "beat resolution_kind must be one of "
                f"{', '.join(RESOLUTION_KINDS)}"
            )
        return beat

    def _canonical_beat(self, beat):
        canonical = _copy(beat)
        if canonical.get("state") == "flag":
            canonical.setdefault("resolution_kind", "delivery")
        return canonical

    def _audience_mode(self, session):
        audience = session.get("audience")
        if not isinstance(audience, dict):
            return None
        mode = audience.get("mode")
        return mode if mode in AUDIENCE_MODES else None

    def _expected_delivery_kind(self, session):
        return {"branch": "commit", "review": "review"}.get(
            self._audience_mode(session)
        )

    def _pr_audience(self, target):
        if target["state"] == "open" and target["merged_at"] is None:
            return {"mode": "review", "why": "the frozen PR is open"}
        return {"mode": "report", "why": "the frozen PR is not open"}

    def _delivery_from_document(
        self, beat, cause_seq=None, kind=None, report=False
    ):
        if beat.get("state") != "accepted":
            return "none", None
        kind = beat.get("delivery_kind") or kind
        artifact = beat.get("landed")
        if artifact:
            return "landed", {
                "artifact": artifact,
                "branch": beat.get("branch"),
                "cause_seq": cause_seq,
                "kind": kind,
            }
        if report:
            return "none", None
        return "pending", {"cause_seq": cause_seq, "kind": kind}

    def _optional_dump(self, value):
        return None if value is None else _dump(value)

    def _session_row(self, db):
        row = db.execute("SELECT * FROM session WHERE singleton = 1").fetchone()
        if row is None:
            raise StoreError("session database has no session row")
        return row

    def _beat_row(self, db, n):
        row = db.execute("SELECT * FROM beats WHERE n = ?", (n,)).fetchone()
        if row is None:
            raise StoreError(f"no beat {n}")
        return row

    def _save_session(
        self,
        db,
        document,
        allow_new_lands=False,
        allow_new_target=False,
        allow_new_branch=False,
        allow_new_execution_policy=False,
        allow_new_audience=False,
    ):
        version, body = self._session_document(document)
        row = self._session_row(db)
        current = json.loads(row["body_json"])
        current_target = current.get("target", _MISSING)
        incoming_target = body.get("target", _MISSING)
        if current_target != incoming_target:
            if current_target is _MISSING and allow_new_target:
                pass
            elif current_target is _MISSING:
                raise Conflict("session target must be recorded through freeze-target")
            else:
                raise Conflict("session target cannot change once frozen")
        current_policy = current.get("execution_policy", _MISSING)
        incoming_policy = body.get("execution_policy", _MISSING)
        if current_policy != incoming_policy:
            if current_policy is _MISSING and allow_new_execution_policy:
                pass
            elif current_policy is _MISSING:
                raise Conflict(
                    "session execution_policy must be recorded through freeze-execution"
                )
            else:
                raise Conflict("session execution_policy cannot change once frozen")
        current_legacy_pr = current.get("legacy_pr", _MISSING)
        incoming_legacy_pr = body.get("legacy_pr", _MISSING)
        if current_legacy_pr != incoming_legacy_pr:
            if current_legacy_pr is _MISSING:
                raise Conflict(
                    "targetless PR sessions must be created through snapshot-pr"
                )
            raise Conflict("session legacy PR identity cannot change")
        if (
            current_legacy_pr is _MISSING
            and current_target is _MISSING
            and _legacy_pr_identity(body) is not None
        ):
            raise Conflict("targetless PR sessions must be created through snapshot-pr")
        if current.get("linked_implementation", _MISSING) != body.get(
            "linked_implementation", _MISSING
        ):
            raise Conflict("session linked implementation provenance cannot change")
        current_branch = current.get("delivery_branch", _MISSING)
        incoming_branch = body.get("delivery_branch", _MISSING)
        if current_target is not _MISSING and current_branch != incoming_branch:
            if current_branch is _MISSING and allow_new_branch:
                pass
            elif current_branch is _MISSING:
                raise Conflict(
                    "session delivery_branch must be recorded through pin-branch"
                )
            else:
                raise Conflict("session delivery_branch cannot change once pinned")
        current_audience = current.get("audience", _MISSING)
        incoming_audience = body.get("audience", _MISSING)
        if current_target is not _MISSING and current_audience != incoming_audience:
            if current_audience is _MISSING and allow_new_audience:
                pass
            else:
                raise Conflict("session audience cannot change once target is frozen")
        current_mode = self._audience_mode(current)
        incoming_mode = self._audience_mode(body)
        if current_mode != incoming_mode:
            has_accept = db.execute(
                "SELECT EXISTS(SELECT 1 FROM actions WHERE kind = 'accept')"
            ).fetchone()[0]
            has_delivery = db.execute(
                "SELECT EXISTS(SELECT 1 FROM beats WHERE delivery_state != 'none')"
            ).fetchone()[0]
            if has_accept or has_delivery:
                raise Conflict("session audience cannot change after actions are recorded")
        current_lands = current.get("lands", [])
        incoming_lands = body.get("lands", [])
        if not isinstance(current_lands, list) or not isinstance(incoming_lands, list):
            raise StoreError("session lands must be a list")
        protected = [
            entry
            for entry in current_lands
            if isinstance(entry, dict) and entry.get("state") == "landed"
        ]
        incoming_landed = [
            entry
            for entry in incoming_lands
            if isinstance(entry, dict) and entry.get("state") == "landed"
        ]
        unlanded = [
            entry
            for entry in incoming_lands
            if not (isinstance(entry, dict) and entry.get("state") == "landed")
        ]
        new_landed = [entry for entry in incoming_landed if entry not in protected]
        if new_landed and not allow_new_lands:
            raise Conflict("landed session entries must be recorded through land")
        if protected or "lands" in body:
            landed = list(protected)
            if allow_new_lands:
                landed.extend(entry for entry in new_landed if entry not in landed)
            body["lands"] = unlanded + landed
        encoded = _dump(body)
        if row["format_version"] == version and row["body_json"] == encoded:
            return False
        db.execute(
            "UPDATE session SET format_version = ?, body_json = ? WHERE singleton = 1",
            (version, encoded),
        )
        return True

    def _save_beat(self, db, document, delivery_state=_MISSING, delivery=_MISSING):
        beat = self._beat_document(document)
        row = db.execute("SELECT * FROM beats WHERE n = ?", (beat["n"],)).fetchone()
        if row is None:
            inferred_state, inferred = self._delivery_from_document(beat)
            state = inferred_state if delivery_state is _MISSING else delivery_state
            detail = inferred if delivery is _MISSING else delivery
            db.execute(
                "INSERT INTO beats VALUES (?, 1, ?, ?, ?)",
                (beat["n"], _dump(beat), state, self._optional_dump(detail)),
            )
            return True, 1, state, detail

        state = row["delivery_state"] if delivery_state is _MISSING else delivery_state
        detail = json.loads(row["delivery_json"]) if row["delivery_json"] else None
        if delivery is not _MISSING:
            detail = delivery
        if state == "landed" and detail:
            beat["landed"] = detail["artifact"]
            beat["delivery_kind"] = detail["kind"]
            if detail.get("branch"):
                beat["branch"] = detail["branch"]
            else:
                beat.pop("branch", None)
        encoded, encoded_detail = _dump(beat), self._optional_dump(detail)
        if (
            row["body_json"] == encoded
            and row["delivery_state"] == state
            and row["delivery_json"] == encoded_detail
        ):
            return False, row["revision"], state, detail
        revision = row["revision"] + 1
        db.execute(
            "UPDATE beats SET revision = ?, body_json = ?, delivery_state = ?, "
            "delivery_json = ? WHERE n = ?",
            (revision, encoded, state, encoded_detail, beat["n"]),
        )
        return True, revision, state, detail

    def _bump_render(self, db):
        db.execute(
            "UPDATE session SET render_revision = render_revision + 1 WHERE singleton = 1"
        )

    def _validate_new_beat(self, beat):
        if beat.get("state") not in OPEN_STATES:
            raise StoreError(
                f"new beat state must be one of {', '.join(OPEN_STATES)}"
            )
        over = beat_budget_problems(beat)
        if over:
            raise StoreError("; ".join(over))
        owned = [
            field
            for field in ("call", "landed", "branch", "delivery_kind", "delivery")
            if field in beat
        ]
        if owned:
            raise StoreError(
                f"new beat cannot set store-owned field {', '.join(owned)}"
            )
        if beat.get("state") == "flag":
            beat.setdefault("resolution_kind", "delivery")

    def put_session(self, document):
        with self._write() as db:
            if db.execute("SELECT EXISTS(SELECT 1 FROM actions)").fetchone()[0]:
                raise Conflict("put-session is only available before actions are recorded")
            changed = self._save_session(db, document)
            if changed:
                self._bump_render(db)
        return self.snapshot()[0]

    def patch_session(self, changes):
        if not isinstance(changes, dict):
            raise StoreError("session patch must be an object")
        patch = _copy(changes)
        if "lands" in patch:
            raise Conflict("session lands must be changed through land")
        if "target" in patch:
            raise Conflict("session target must be changed through freeze-target")
        if "execution_policy" in patch:
            raise Conflict(
                "session execution_policy must be changed through freeze-execution"
            )
        if "legacy_pr" in patch:
            raise Conflict("session legacy PR identity cannot change")
        if "delivery_branch" in patch:
            raise Conflict(
                "session delivery_branch must be changed through pin-branch"
            )
        if "schema_version" in patch:
            version = patch.pop("schema_version")
            if type(version) is not int or version != SCHEMA_VERSION:
                raise StoreError(f"session schema_version must be {SCHEMA_VERSION}")
        with self._write() as db:
            current = json.loads(self._session_row(db)["body_json"])
            if current.get("linked_implementation") is not None:
                raise Conflict("linked implementation session metadata is immutable")
            current.update(patch)
            changed = self._save_session(db, current)
            if changed:
                self._bump_render(db)
        return self.snapshot()[0]

    def pin_branch(self, branch):
        if not isinstance(branch, str) or not branch.strip():
            raise StoreError("delivery branch must be non-empty text")
        branch = branch.strip()
        if any(ord(character) < 32 for character in branch):
            raise StoreError("delivery branch contains a control character")
        with self._write() as db:
            current = json.loads(self._session_row(db)["body_json"])
            target = current.get("target")
            if target is None:
                raise Conflict("session has no frozen target")
            if self._expected_delivery_kind(current) != "commit":
                raise Conflict("session is not in branch delivery mode")
            frozen = current.get("delivery_branch")
            if frozen is not None:
                if frozen != branch:
                    raise Conflict("session delivery_branch cannot change once pinned")
            else:
                if target["state"] == "open" and target["merged_at"] is None:
                    if branch != target["head_ref"]:
                        raise Conflict(
                            "open PR delivery branch must match the frozen head ref"
                        )
                has_landed = db.execute(
                    "SELECT EXISTS(SELECT 1 FROM beats WHERE delivery_state = 'landed')"
                ).fetchone()[0]
                if has_landed:
                    raise Conflict(
                        "delivery branch must be pinned before the first landing"
                    )
                current["delivery_branch"] = branch
                if self._save_session(db, current, allow_new_branch=True):
                    self._bump_render(db)
        return self.snapshot()[0]

    def freeze_target(
        self,
        document,
        diff_source,
        metadata_source,
        trusted_context_source=None,
        object_bundle_source=None,
    ):
        target = _copy(document)
        for name in (
            "diff_sha256",
            "diff_bytes",
            "trusted_context_sha256",
            "trusted_context_bytes",
            "object_bundle_sha256",
            "object_bundle_bytes",
        ):
            if name in target:
                raise StoreError(f"freeze-target computes {name}")

        staged = []
        with self._session_lock():
            try:
                with self._read() as db:
                    current = json.loads(self._session_row(db)["body_json"])
                    if current.get("linked_implementation") is not None:
                        raise Conflict("linked implementation target projections are immutable")
                diff, diff_sha256, diff_bytes = _stage_copy(
                    diff_source, self.root, "pr.diff"
                )
                staged.append(diff)
                metadata, _metadata_sha256, _metadata_bytes = _stage_copy(
                    metadata_source, self.root, "pr.json"
                )
                staged.append(metadata)
                trusted_context = None
                if trusted_context_source is not None:
                    trusted_context, context_sha256, context_bytes = _stage_copy(
                        trusted_context_source, self.root, "trusted-context.json"
                    )
                    staged.append(trusted_context)
                    target.update({
                        "trusted_context_sha256": context_sha256,
                        "trusted_context_bytes": context_bytes,
                    })
                object_bundle = None
                if object_bundle_source is not None:
                    object_bundle, bundle_sha256, bundle_bytes = _stage_copy(
                        object_bundle_source, self.root, "pr.bundle"
                    )
                    staged.append(object_bundle)
                    target.update({
                        "object_bundle_sha256": bundle_sha256,
                        "object_bundle_bytes": bundle_bytes,
                    })
                target.update({
                    "diff_sha256": diff_sha256,
                    "diff_bytes": diff_bytes,
                })
                target = self._target_document(target)
                if trusted_context is not None:
                    self._trusted_context_document(
                        trusted_context.read_bytes(), target
                    )

                with self._read() as db:
                    current = json.loads(self._session_row(db)["body_json"])
                    frozen = current.get("target")
                    if frozen is not None and frozen != target:
                        raise Conflict("session target cannot change once frozen")
                    if frozen is None:
                        has_work = db.execute(
                            "SELECT EXISTS(SELECT 1 FROM beats) OR "
                            "EXISTS(SELECT 1 FROM actions)"
                        ).fetchone()[0]
                        if has_work:
                            raise Conflict(
                                "session target must be frozen before beats or actions"
                            )

                os.replace(str(metadata), str(self.root / "pr.json"))
                staged.remove(metadata)
                if trusted_context is not None:
                    os.replace(
                        str(trusted_context), str(self.root / "trusted-context.json")
                    )
                    staged.remove(trusted_context)
                if object_bundle is not None:
                    os.replace(str(object_bundle), str(self.root / "pr.bundle"))
                    staged.remove(object_bundle)
                os.replace(str(diff), str(self.root / "pr.diff"))
                staged.remove(diff)
                _fsync_directory(self.root)

                with self._write() as db:
                    current = json.loads(self._session_row(db)["body_json"])
                    frozen = current.get("target")
                    if frozen is None:
                        has_work = db.execute(
                            "SELECT EXISTS(SELECT 1 FROM beats) OR "
                            "EXISTS(SELECT 1 FROM actions)"
                        ).fetchone()[0]
                        if has_work:
                            raise Conflict(
                                "session target must be frozen before beats or actions"
                            )
                        current["target"] = target
                        current["execution_policy"] = self._execution_policy(
                            target, "no_exec"
                        )
                        current["audience"] = self._pr_audience(target)
                        current.pop("delivery_branch", None)
                        if self._save_session(
                            db,
                            current,
                            allow_new_target=True,
                            allow_new_execution_policy=True,
                        ):
                            self._bump_render(db)
                    elif frozen != target:
                        raise Conflict("session target cannot change once frozen")
                    else:
                        allow_new_policy = "execution_policy" not in current
                        allow_new_audience = "audience" not in current
                        if allow_new_audience:
                            has_work = db.execute(
                                "SELECT EXISTS(SELECT 1 FROM beats) OR "
                                "EXISTS(SELECT 1 FROM actions)"
                            ).fetchone()[0]
                            if not has_work:
                                current["audience"] = self._pr_audience(target)
                            else:
                                allow_new_audience = False
                        if allow_new_policy:
                            current["execution_policy"] = self._execution_policy(
                                target, "no_exec"
                            )
                        if self._save_session(
                            db,
                            current,
                            allow_new_execution_policy=allow_new_policy,
                            allow_new_audience=allow_new_audience,
                        ):
                            self._bump_render(db)
            finally:
                for path in staged:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
        return target

    def frozen_target(self):
        with self._read() as db:
            session = json.loads(self._session_row(db)["body_json"])
            target = session.get("target")
            if target is None:
                raise Conflict("session has no frozen target")
            return target

    def freeze_execution(self, mode):
        with self._write() as db:
            current = json.loads(self._session_row(db)["body_json"])
            target = current.get("target")
            if target is None:
                raise Conflict("session has no frozen target")
            desired = self._execution_policy(target, mode)
            frozen = current.get("execution_policy")
            if frozen is not None:
                frozen = self._execution_policy_document(frozen, target)
                if frozen != desired:
                    raise Conflict(
                        "session execution_policy cannot change once frozen"
                    )
                return frozen
            has_work = db.execute(
                "SELECT EXISTS(SELECT 1 FROM beats) OR "
                "EXISTS(SELECT 1 FROM actions)"
            ).fetchone()[0]
            if has_work:
                raise Conflict(
                    "session execution_policy must be frozen before beats or actions"
                )
            current["execution_policy"] = desired
            if self._save_session(
                db, current, allow_new_execution_policy=True
            ):
                self._bump_render(db)
            return desired

    def _required_execution_policy(self, session):
        target = session.get("target")
        if target is None:
            raise Conflict("session has no frozen target")
        policy = session.get("execution_policy")
        if policy is None:
            raise Conflict("session has no frozen execution policy")
        return self._execution_policy_document(
            policy, target, session.get("linked_implementation")
        )

    def _legacy_pr_execution_policy(self, session):
        identity = session.get("legacy_pr")
        if not isinstance(identity, dict):
            return None
        return {
            "version": EXECUTION_POLICY_VERSION,
            "trust": "untrusted",
            "mode": "no_exec",
            "legacy": True,
        }

    def _session_execution_policy(self, db):
        session = json.loads(self._session_row(db)["body_json"])
        if session.get("target") is None:
            return session, self._legacy_pr_execution_policy(session)
        return session, self._required_execution_policy(session)

    def _replacement_reason(self, session, policy):
        if policy is not None and policy.get("legacy"):
            return "legacy PR session has no frozen target"
        target = session.get("target")
        if target is not None and "trusted_context_sha256" not in target:
            return "PR session has no frozen trusted context"
        linked = session.get("linked_implementation")
        if linked is not None:
            try:
                self._linked_implementation_document(linked)
                if self._audience_mode(session) != "branch":
                    return "linked implementation audience is invalid"
                if policy is None or policy.get("mode") != "gateway_attested":
                    return "linked implementation execution policy is invalid"
            except StoreError as error:
                return str(error)
            return None
        if (
            target is not None
            and self._audience_mode(session) != self._pr_audience(target)["mode"]
        ):
            return "PR session audience does not match its frozen lifecycle"
        return None

    def replacement_reason(self):
        with self._read() as db:
            session = json.loads(self._session_row(db)["body_json"])
            target = session.get("target")
            policy = (
                self._legacy_pr_execution_policy(session)
                if target is None
                else session.get("execution_policy")
            )
            reason = self._replacement_reason(session, policy)
            if reason:
                return reason
            if target is not None:
                try:
                    self._required_execution_policy(session)
                except StoreError as error:
                    return str(error)
            return None

    def check_execution(self):
        target = self.verify_target_files()
        with self._read() as db:
            session = json.loads(self._session_row(db)["body_json"])
            policy = self._required_execution_policy(session)
        if policy["target"] != self._execution_target(target):
            raise Conflict("session execution_policy target does not match")
        if policy["mode"] == "no_exec":
            raise Conflict("session execution policy forbids target code execution")
        if policy["mode"] == "gateway_attested":
            raise Conflict(
                "gateway-attested sessions permit only verified output application"
            )
        self.read_trusted_context()
        return policy

    # ---- linked implementation authority ----------------------------

    def _implementation_link_document(self, row):
        return {
            "version": 1,
            "link_id": row["link_id"],
            "state": row["state"],
            "source_session_id": row["source_session_id"]
            if "source_session_id" in row.keys()
            else None,
            "source_action_seq": row["source_action_seq"],
            "source_action_id": row["source_action_id"],
            "source_beat": row["source_beat"],
            "source_beat_revision": row["source_beat_revision"],
            "source_beat_sha256": row["source_beat_sha256"],
            "target_sha256": row["target_sha256"],
            "actor": row["actor"],
            "approval": row["approval"],
            "child_session_id": row["child_session_id"],
            "child_path": row["child_path"],
            "branch": row["branch"],
            "created_at": row["created_at"],
            "ready_at": row["ready_at"],
        }

    def _link_row(self, db, link_id):
        row = db.execute(
            "SELECT links.*, session.session_id AS source_session_id "
            "FROM implementation_links AS links JOIN session ON session.singleton = 1 "
            "WHERE links.link_id = ?",
            (link_id,),
        ).fetchone()
        if row is None:
            raise StoreError(f"no implementation link {link_id}")
        return row

    def implementation_link(self, link_id):
        if not isinstance(link_id, str) or not _SHA256.fullmatch(link_id):
            raise StoreError("implementation link id must be a SHA-256 digest")
        with self._read() as db:
            return self._implementation_link_document(self._link_row(db, link_id))

    def authorize_implementation(self, source_action_seq, source_beat, actor, approval):
        _positive(source_action_seq, "source action seq")
        _positive(source_beat, "source beat")
        if not isinstance(actor, str) or not actor.strip():
            raise StoreError("implementation actor must be non-empty text")
        if not isinstance(approval, str) or not approval.strip():
            raise StoreError("implementation approval must be non-empty text")
        actor, approval = actor.strip(), approval.strip()
        target = self.verify_target_files()
        if "trusted_context_sha256" not in target:
            raise Conflict("implementation requires frozen trusted context")
        if "object_bundle_sha256" not in target:
            raise Conflict("implementation requires a frozen Git object bundle")
        with self._write() as db:
            session_row = self._session_row(db)
            session = json.loads(session_row["body_json"])
            policy = self._required_execution_policy(session)
            if self._audience_mode(session) not in ("review", "report"):
                raise Conflict("implementation authorization requires review or report audience")
            if policy["mode"] != "no_exec":
                raise Conflict("source session must retain its no_exec policy")
            if session.get("target") != target:
                raise Conflict("source target changed during implementation authorization")
            action = self._action_row(db, source_action_seq)
            if action["kind"] != "accept" or action["beat_n"] != source_beat:
                raise Conflict(
                    f"action {source_action_seq} is not the accept for beat {source_beat}"
                )
            if action["state"] != "acked":
                raise Conflict(
                    f"accept action {source_action_seq} must be acknowledged first"
                )
            beat_row = self._beat_row(db, source_beat)
            beat = json.loads(beat_row["body_json"])
            if beat.get("state") != "accepted":
                raise Conflict(f"beat {source_beat} is not accepted")
            beat_json = _dump(beat)
            beat_sha256 = hashlib.sha256(beat_json.encode("utf-8")).hexdigest()
            target_sha256 = hashlib.sha256(_dump(target).encode("utf-8")).hexdigest()
            bound = {
                "version": 1,
                "sourceSessionId": session_row["session_id"],
                "sourceActionSeq": source_action_seq,
                "sourceActionId": action["action_id"],
                "sourceBeat": source_beat,
                "sourceBeatRevision": beat_row["revision"],
                "sourceBeatSha256": beat_sha256,
                "targetSha256": target_sha256,
                "actor": actor,
                "approval": approval,
            }
            link_id = hashlib.sha256(_dump(bound).encode("utf-8")).hexdigest()
            child_path = f"implementations/{link_id}"
            branch = f"underwrite/implementation-{link_id[:16]}"
            existing = db.execute(
                "SELECT links.*, session.session_id AS source_session_id "
                "FROM implementation_links AS links JOIN session ON session.singleton = 1 "
                "WHERE links.source_action_seq = ?",
                (source_action_seq,),
            ).fetchone()
            if existing is not None:
                expected = (
                    link_id, action["action_id"], source_beat, beat_row["revision"],
                    beat_json, beat_sha256, target_sha256, actor, approval,
                    child_path, branch,
                )
                actual = (
                    existing["link_id"], existing["source_action_id"],
                    existing["source_beat"], existing["source_beat_revision"],
                    existing["source_beat_json"], existing["source_beat_sha256"],
                    existing["target_sha256"], existing["actor"],
                    existing["approval"], existing["child_path"], existing["branch"],
                )
                if actual != expected:
                    raise Conflict(
                        f"action {source_action_seq} already has a different implementation authorization"
                    )
                return self._implementation_link_document(existing)
            db.execute(
                "INSERT INTO implementation_links "
                "(link_id, source_action_seq, source_action_id, source_beat, "
                "source_beat_revision, source_beat_json, source_beat_sha256, "
                "target_sha256, actor, approval, child_path, branch, state, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?)",
                (
                    link_id, source_action_seq, action["action_id"], source_beat,
                    beat_row["revision"], beat_json, beat_sha256, target_sha256,
                    actor, approval, child_path, branch, _now(),
                ),
            )
            return self._implementation_link_document(self._link_row(db, link_id))

    def _linked_projection_identities(self, target):
        expected = {
            "pr.diff": (target["diff_sha256"], target["diff_bytes"]),
        }
        if "trusted_context_sha256" in target:
            expected["trusted-context.json"] = (
                target["trusted_context_sha256"], target["trusted_context_bytes"]
            )
        if "object_bundle_sha256" in target:
            expected["pr.bundle"] = (
                target["object_bundle_sha256"], target["object_bundle_bytes"]
            )
        metadata = self.root / "pr.json"
        try:
            expected["pr.json"] = _file_identity(metadata)
        except FileNotFoundError as error:
            raise Conflict("frozen target projection pr.json is missing") from error
        return expected

    def _copy_linked_projections(self, child_root, target):
        implementation_root = child_root.parent
        for directory, label in (
            (implementation_root, "implementation root"),
            (child_root, "linked child path"),
        ):
            created = False
            try:
                directory.mkdir(mode=0o700)
                created = True
            except FileExistsError:
                pass
            try:
                details = directory.lstat()
            except OSError as error:
                raise Conflict(f"{label} cannot be inspected") from error
            if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
                raise Conflict(f"{label} must be a real directory")
            if created:
                _fsync_directory(directory.parent)
        expected = self._linked_projection_identities(target)
        staged = []
        try:
            for name, identity in expected.items():
                try:
                    path, digest, size = _stage_copy(
                        self.root / name, child_root, name
                    )
                except FileNotFoundError as error:
                    raise Conflict(f"frozen target projection {name} is missing") from error
                staged.append((path, name))
                if (digest, size) != identity:
                    raise Conflict(f"frozen target projection {name} does not match")
            for path, name in staged:
                os.replace(str(path), str(child_root / name))
            staged.clear()
            _fsync_directory(child_root)
        finally:
            for path, _name in staged:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    def _linked_child_body(self, link, target):
        provenance = {
            "version": 1,
            "link_id": link["link_id"],
            "source_session_id": link["source_session_id"],
            "source_action_seq": link["source_action_seq"],
            "source_action_id": link["source_action_id"],
            "source_beat": link["source_beat"],
            "source_beat_revision": link["source_beat_revision"],
            "source_beat_sha256": link["source_beat_sha256"],
            "target_sha256": link["target_sha256"],
            "actor": link["actor"],
            "approval": link["approval"],
        }
        return {
            "repo": target["repo"],
            "cursor": link["source_beat"],
            "lands": [],
            "audience": {
                "mode": "branch",
                "why": "approved implementation of a frozen PR finding",
            },
            "delivery_branch": link["branch"],
            "target": _copy(target),
            "execution_policy": self._execution_policy(
                target, "gateway_attested", link["link_id"]
            ),
            "linked_implementation": provenance,
        }

    def _guard_existing_linked_child(self, child_root, link, target):
        try:
            child_details = child_root.lstat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise Conflict("linked child path cannot be inspected") from error
        if not stat.S_ISDIR(child_details.st_mode) or stat.S_ISLNK(
            child_details.st_mode
        ):
            raise Conflict("linked child path must be a real directory")

        database = child_root / "session.sqlite3"
        lock = child_root / ".session.lock"
        for path, label in (
            (database, "linked child database"),
            (lock, "linked child lock"),
        ):
            try:
                details = path.lstat()
            except OSError as error:
                raise Conflict(f"{label} cannot be inspected") from error
            if (
                not stat.S_ISREG(details.st_mode)
                or stat.S_ISLNK(details.st_mode)
                or details.st_nlink != 1
            ):
                raise Conflict(f"{label} must be one real regular file")

        journal = child_root / "session.sqlite3-journal"
        try:
            journal.lstat()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise Conflict("linked child journal cannot be inspected") from error
        else:
            raise Conflict("linked child has an unfinished journal")

        for name, identity in self._linked_projection_identities(target).items():
            path = child_root / name
            try:
                details = path.lstat()
            except OSError as error:
                raise Conflict(
                    f"linked child projection {name} cannot be inspected"
                ) from error
            if (
                not stat.S_ISREG(details.st_mode)
                or stat.S_ISLNK(details.st_mode)
                or details.st_nlink != 1
                or _file_identity(path) != identity
            ):
                raise Conflict(f"linked child projection {name} changed")

        db = None
        try:
            uri = database.resolve().as_uri() + "?mode=ro&immutable=1"
            db = sqlite3.connect(uri, uri=True)
            version = db.execute("PRAGMA user_version").fetchone()[0]
            row = db.execute(
                "SELECT session_id, format_version, body_json "
                "FROM session WHERE singleton = 1"
            ).fetchone()
            counts = db.execute(
                "SELECT (SELECT COUNT(*) FROM actions), "
                "(SELECT COUNT(*) FROM beats)"
            ).fetchone()
        except (OSError, sqlite3.Error) as error:
            raise Conflict("linked child database cannot be read safely") from error
        finally:
            if db is not None:
                db.close()
        if row is None:
            raise Conflict("linked child database has no session")
        if version != DB_SCHEMA_VERSION or row[1] != SCHEMA_VERSION:
            raise Conflict("linked child database has an unsupported version")
        try:
            body = json.loads(row[2])
        except (TypeError, json.JSONDecodeError) as error:
            raise Conflict("linked child session is malformed") from error
        expected = self._linked_child_body(link, target)
        if isinstance(body, dict):
            body = _copy(body)
            body["lands"] = []
        if body != expected:
            raise Conflict("linked child path already contains another session")
        if tuple(counts) != (1, 1):
            raise Conflict("linked child session does not contain its exact finding")
        if link.get("child_session_id") not in (None, row[0]):
            raise Conflict("linked child session identity changed")
        return row[0]

    def _initialize_linked_child(self, child, link, target):
        desired_body = self._linked_child_body(link, target)
        source_beat = json.loads(link["source_beat_json"])
        child_beat = _copy(source_beat)
        for name in ("landed", "branch", "delivery_kind", "delivery"):
            child_beat.pop(name, None)
        child_beat["state"] = "accepted"
        child_beat["call"] = link["approval"]
        timestamp = _now()
        with child._write() as db:
            row = child._session_row(db)
            current = json.loads(row["body_json"])
            if current.get("linked_implementation") is not None:
                fixed = _copy(current)
                fixed["lands"] = []
                if fixed != desired_body:
                    raise Conflict("linked child session has different immutable provenance")
                action = db.execute("SELECT * FROM actions WHERE seq = 1").fetchone()
                beat = db.execute(
                    "SELECT * FROM beats WHERE n = ?", (link["source_beat"],)
                ).fetchone()
                if action is None or beat is None:
                    raise Conflict("linked child session initialization is incomplete")
                counts = db.execute(
                    "SELECT (SELECT COUNT(*) FROM actions), "
                    "(SELECT COUNT(*) FROM beats)"
                ).fetchone()
                actual_beat = json.loads(beat["body_json"])
                for name in ("landed", "branch", "delivery_kind", "delivery"):
                    actual_beat.pop(name, None)
                expected_application = child._application(
                    {
                        "kind": "beat",
                        "n": child_beat["n"],
                        "state": "accepted",
                        "call": link["approval"],
                    },
                    desired_body,
                    (child_beat,),
                )
                if (
                    tuple(counts) != (1, 1)
                    or actual_beat != child_beat
                    or action["action_id"] != f"implementation:{link['link_id']}"
                    or action["beat_n"] != child_beat["n"]
                    or action["kind"] != "accept"
                    or action["note"] != link["approval"]
                    or action["state"] not in ("applied", "acked")
                    or json.loads(action["result_json"]) != expected_application
                ):
                    raise Conflict("linked child session does not contain its exact finding")
                return row["session_id"]
            has_work = db.execute(
                "SELECT EXISTS(SELECT 1 FROM beats) OR EXISTS(SELECT 1 FROM actions)"
            ).fetchone()[0]
            if current or has_work:
                raise Conflict("linked child path already contains another session")
            child._session_document(desired_body)
            db.execute(
                "UPDATE session SET body_json = ?, render_revision = 1 WHERE singleton = 1",
                (_dump(desired_body),),
            )
            delivery = {
                "cause_seq": 1,
                "kind": "commit",
            }
            db.execute(
                "INSERT INTO beats "
                "(n, revision, body_json, delivery_state, delivery_json) "
                "VALUES (?, 1, ?, 'pending', ?)",
                (child_beat["n"], _dump(child_beat), _dump(delivery)),
            )
            result = {
                "kind": "beat",
                "n": child_beat["n"],
                "state": "accepted",
                "call": link["approval"],
            }
            application = child._application(result, desired_body, (child_beat,))
            child._insert_action(
                db,
                1,
                f"implementation:{link['link_id']}",
                child_beat["n"],
                "accept",
                link["approval"],
                "applied",
                application,
                evidence="linked implementation authorization",
                produced_at=timestamp,
                applied_at=timestamp,
            )
            return row["session_id"]

    def create_linked_implementation(self, link_id):
        link = self.implementation_link(link_id)
        with self._read() as db:
            live = self._link_row(db, link_id)
            action = self._action_row(db, live["source_action_seq"])
            beat = self._beat_row(db, live["source_beat"])
            session_row = self._session_row(db)
            session = json.loads(session_row["body_json"])
            target = session.get("target")
            if action["state"] != "acked" or action["kind"] != "accept":
                raise Conflict("implementation source accept is no longer acknowledged")
            if action["beat_n"] != live["source_beat"]:
                raise Conflict("implementation source action no longer matches its beat")
            if beat["revision"] != live["source_beat_revision"]:
                raise Conflict("implementation source beat revision moved")
            if beat["body_json"] != live["source_beat_json"]:
                raise Conflict("implementation source beat content moved")
            if hashlib.sha256(beat["body_json"].encode("utf-8")).hexdigest() != live["source_beat_sha256"]:
                raise Conflict("implementation source beat digest does not match")
            if hashlib.sha256(_dump(target).encode("utf-8")).hexdigest() != live["target_sha256"]:
                raise Conflict("implementation source target moved")
            link = self._implementation_link_document(live)
            link["source_beat_json"] = live["source_beat_json"]
        if self.verify_target_files() != target:
            raise Conflict("implementation source target moved")
        child_root = self.root / link["child_path"]
        existing_session_id = self._guard_existing_linked_child(
            child_root, link, target
        )
        if existing_session_id is None:
            implementation_root = child_root.parent
            created_root = False
            try:
                implementation_root.mkdir(mode=0o700)
                created_root = True
            except FileExistsError:
                pass
            try:
                root_details = implementation_root.lstat()
            except OSError as error:
                raise Conflict("implementation root cannot be inspected") from error
            if not stat.S_ISDIR(root_details.st_mode) or stat.S_ISLNK(
                root_details.st_mode
            ):
                raise Conflict("implementation root must be a real directory")
            if created_root:
                _fsync_directory(implementation_root.parent)

            staging = Path(
                tempfile.mkdtemp(
                    prefix=f".{link['link_id']}.",
                    suffix=".tmp",
                    dir=str(implementation_root),
                )
            )
            try:
                self._copy_linked_projections(staging, target)
                staged_child = SessionStore(staging)
                self._initialize_linked_child(staged_child, link, target)
                staged_child.verify_target_files()
                _fsync_directory(staging)
                try:
                    os.rename(staging, child_root)
                except OSError as error:
                    if self._guard_existing_linked_child(
                        child_root, link, target
                    ) is None:
                        raise Conflict("linked child could not be published") from error
                else:
                    _fsync_directory(implementation_root)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)

        child = SessionStore(child_root)
        child_session_id = self._initialize_linked_child(child, link, target)
        child.verify_target_files()
        return {
            "link": self.implementation_link(link_id),
            "child_root": str(child_root.resolve()),
            "child_session_id": child_session_id,
            "action_seq": 1,
            "beat": link["source_beat"],
            "branch": link["branch"],
        }

    def complete_implementation_link(self, link_id, child_session_id):
        if not isinstance(child_session_id, str) or not child_session_id.strip():
            raise StoreError("child_session_id must be non-empty text")
        child_session_id = child_session_id.strip()
        link = self.implementation_link(link_id)
        target = self.verify_target_files()
        child_root = self.root / link["child_path"]
        for directory, label in (
            (child_root.parent, "implementation root"),
            (child_root, "linked child path"),
        ):
            try:
                details = directory.lstat()
            except OSError as error:
                raise Conflict(f"{label} cannot be inspected") from error
            if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
                raise Conflict(f"{label} must be a real directory")
        self._guard_existing_linked_child(child_root, link, target)
        child = SessionStore(child_root)
        with child._read() as child_db:
            child_row = child._session_row(child_db)
            child_body = json.loads(child_row["body_json"])
            if child_row["session_id"] != child_session_id:
                raise Conflict("linked child session identity does not match")
            expected = self._linked_child_body(link, target)
            fixed = _copy(child_body)
            fixed["lands"] = []
            if fixed != expected:
                raise Conflict("linked child provenance does not match")
            child._required_execution_policy(child_body)
        if child.verify_target_files() != target:
            raise Conflict("linked child target does not match its source")
        with self._write() as db:
            row = self._link_row(db, link_id)
            if row["state"] == "ready":
                if row["child_session_id"] != child_session_id:
                    raise Conflict("implementation link names a different child session")
                return self._implementation_link_document(row)
            if row["child_session_id"] not in (None, child_session_id):
                raise Conflict("implementation link names a different child session")
            db.execute(
                "UPDATE implementation_links SET state = 'ready', "
                "child_session_id = ?, ready_at = ? WHERE link_id = ?",
                (child_session_id, _now(), link_id),
            )
            return self._implementation_link_document(self._link_row(db, link_id))

    def _trusted_implementation_profile(self, document):
        if not isinstance(document, dict) or set(document) != _TRUSTED_PROFILE_FIELDS:
            raise StoreError("trusted profile has an unsupported shape")
        profile = _copy(document)
        if type(profile["version"]) is not int or profile["version"] != 1:
            raise StoreError("trusted profile version must be 1")
        key_id = profile["keyId"]
        if (
            not isinstance(key_id, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", key_id)
        ):
            raise StoreError("trusted profile keyId must name a SHA-256 key fingerprint")
        for name in ("signerId", "executorId"):
            value = profile[name]
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise StoreError(f"trusted profile {name} must be non-empty text")
        for name in ("job", "sandbox"):
            if not isinstance(profile[name], dict):
                raise StoreError(f"trusted profile {name} must be an object")
        _non_negative(profile["exitCode"], "trusted profile exitCode")
        if profile["exitCode"] > 255:
            raise StoreError("trusted profile exitCode must not exceed 255")
        try:
            encoded = _dump(profile).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise StoreError("trusted profile must be canonical JSON data") from error
        if len(encoded) > MAX_IMPLEMENTATION_PROFILE_BYTES:
            raise StoreError("trusted profile exceeds the implementation byte limit")
        return profile

    def _linked_attempt_context(self, db, action_seq):
        action_seq = _positive(action_seq, "action seq")
        session_row = self._session_row(db)
        session = json.loads(session_row["body_json"])
        linked = session.get("linked_implementation")
        if linked is None:
            raise Conflict("session is not a linked implementation")
        linked = self._linked_implementation_document(linked)
        policy = self._required_execution_policy(session)
        if policy["mode"] != "gateway_attested":
            raise Conflict("linked implementation has no attested execution policy")
        action = self._action_row(db, action_seq)
        if (
            action["seq"] != 1
            or action["kind"] != "accept"
            or action["beat_n"] != linked["source_beat"]
            or action["state"] not in ("applied", "acked")
        ):
            raise Conflict("action is not the linked implementation accept")
        return session_row, session, linked, action

    def _implementation_attempt_document(self, db, row):
        action = self._action_row(db, row["action_seq"])
        return {
            "version": 1,
            "action_seq": row["action_seq"],
            "beat": action["beat_n"],
            "attempt": row["attempt"],
            "state": row["state"],
            "challenge": row["challenge"],
            "request": json.loads(row["request_json"]),
            "request_sha256": row["request_sha256"],
            "trusted_profile": json.loads(row["trusted_profile_json"]),
            "capability": row["capability"],
            "receipt": row["receipt"],
            "evidence": None if row["evidence_json"] is None else json.loads(row["evidence_json"]),
            "commit_plan": None if row["commit_plan_json"] is None else json.loads(row["commit_plan_json"]),
            "failure": row["failure"],
            "created_at": row["created_at"],
            "verified_at": row["verified_at"],
            "prepared_at": row["prepared_at"],
            "landed_at": row["landed_at"],
            "failed_at": row["failed_at"],
        }

    def _attempt_row(self, db, action_seq, attempt=None):
        _positive(action_seq, "action seq")
        if attempt is None:
            row = db.execute(
                "SELECT * FROM implementation_attempts WHERE action_seq = ? "
                "ORDER BY attempt DESC LIMIT 1",
                (action_seq,),
            ).fetchone()
        else:
            _positive(attempt, "attempt")
            row = db.execute(
                "SELECT * FROM implementation_attempts "
                "WHERE action_seq = ? AND attempt = ?",
                (action_seq, attempt),
            ).fetchone()
        if row is None:
            suffix = "" if attempt is None else f" attempt {attempt}"
            raise StoreError(f"action {action_seq} has no implementation{suffix}")
        return row

    def implementation_attempt(self, action_seq, attempt=None):
        with self._read() as db:
            self._linked_attempt_context(db, action_seq)
            return self._implementation_attempt_document(
                db, self._attempt_row(db, action_seq, attempt)
            )

    def reserve_implementation_attempt(self, action_seq, trusted_profile):
        profile = self._trusted_implementation_profile(trusted_profile)
        profile_json = _dump(profile)
        with self._write() as db:
            session_row, session, linked, action = self._linked_attempt_context(
                db, action_seq
            )
            latest = db.execute(
                "SELECT * FROM implementation_attempts WHERE action_seq = ? "
                "ORDER BY attempt DESC LIMIT 1",
                (action_seq,),
            ).fetchone()
            if latest is not None:
                if latest["trusted_profile_json"] != profile_json:
                    raise Conflict("implementation attempt already binds a different trusted profile")
                if latest["state"] != "failed":
                    return self._implementation_attempt_document(db, latest)
                number = latest["attempt"] + 1
            else:
                number = 1
            challenge = secrets.token_hex(32)
            request = {
                "version": 1,
                "sessionId": session_row["session_id"],
                "challenge": challenge,
                "target": _copy(session["target"]),
                "action": {
                    "seq": action_seq,
                    "beat": action["beat_n"],
                    "attempt": number,
                },
                "job": _copy(profile["job"]),
                "sandbox": _copy(profile["sandbox"]),
                "exitCode": profile["exitCode"],
            }
            request_json = _dump(request)
            if len(request_json.encode("utf-8")) > MAX_IMPLEMENTATION_REQUEST_BYTES:
                raise StoreError("implementation request exceeds its byte limit")
            request_sha256 = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
            timestamp = _now()
            db.execute(
                "INSERT INTO implementation_attempts "
                "(action_seq, attempt, challenge, state, trusted_profile_json, "
                "request_json, request_sha256, created_at) "
                "VALUES (?, ?, ?, 'reserved', ?, ?, ?, ?)",
                (
                    action_seq, number, challenge, profile_json, request_json,
                    request_sha256, timestamp,
                ),
            )
            beat_row = self._beat_row(db, action["beat_n"])
            beat = json.loads(beat_row["body_json"])
            detail = {
                "cause_seq": action_seq,
                "kind": "commit",
                "attempt": number,
            }
            changed, _revision, _state, _detail = self._save_beat(
                db, beat, "pending", detail
            )
            if changed:
                self._bump_render(db)
            return self._implementation_attempt_document(
                db, self._attempt_row(db, action_seq, number)
            )

    def _artifact_descriptor(self, document, label):
        if not isinstance(document, dict) or set(document) != {"sha256", "bytes"}:
            raise StoreError(f"implementation evidence {label} has an unsupported shape")
        value = _copy(document)
        if not isinstance(value["sha256"], str) or not _SHA256.fullmatch(value["sha256"]):
            raise StoreError(f"implementation evidence {label} digest is invalid")
        _positive(value["bytes"], f"implementation evidence {label} bytes")
        return value

    def _stream_descriptor(self, document, label):
        if not isinstance(document, dict) or set(document) != {"sha256", "bytes", "truncated"}:
            raise StoreError(f"implementation evidence {label} has an unsupported shape")
        value = _copy(document)
        if not isinstance(value["sha256"], str) or not _SHA256.fullmatch(value["sha256"]):
            raise StoreError(f"implementation evidence {label} digest is invalid")
        _non_negative(value["bytes"], f"implementation evidence {label} bytes")
        if value["truncated"] is not False:
            raise StoreError(f"implementation evidence {label} must not be truncated")
        return value

    def _implementation_evidence(self, document, attempt, capability, receipt):
        if not isinstance(document, dict) or set(document) != _IMPLEMENTATION_EVIDENCE_FIELDS:
            raise StoreError("implementation evidence has an unsupported shape")
        evidence = _copy(document)
        profile = json.loads(attempt["trusted_profile_json"])
        if type(evidence["version"]) is not int or evidence["version"] != 1:
            raise StoreError("implementation evidence version must be 1")
        expected = {
            "requestSha256": attempt["request_sha256"],
            "keyId": profile["keyId"],
            "signerId": profile["signerId"],
            "executorId": profile["executorId"],
            "capabilitySha256": hashlib.sha256(capability).hexdigest(),
            "receiptSha256": hashlib.sha256(receipt).hexdigest(),
            "exitCode": profile["exitCode"],
        }
        for name, value in expected.items():
            if evidence[name] != value:
                raise Conflict(f"implementation evidence {name} does not match")
        for name in (
            "requestSha256", "capabilitySha256", "receiptSha256",
            "inputTree", "outputTree",
        ):
            if not isinstance(evidence[name], str) or not _SHA256.fullmatch(evidence[name]):
                raise StoreError(f"implementation evidence {name} is invalid")
        evidence["outputBundle"] = self._artifact_descriptor(
            evidence["outputBundle"], "outputBundle"
        )
        evidence["stdout"] = self._stream_descriptor(evidence["stdout"], "stdout")
        evidence["stderr"] = self._stream_descriptor(evidence["stderr"], "stderr")
        return evidence

    def record_verified_implementation(
        self, action_seq, attempt, capability, receipt, evidence
    ):
        if not isinstance(capability, bytes) or not capability:
            raise StoreError("verified capability must be non-empty bytes")
        if not isinstance(receipt, bytes) or not receipt:
            raise StoreError("verified receipt must be non-empty bytes")
        with self._write() as db:
            self._linked_attempt_context(db, action_seq)
            row = self._attempt_row(db, action_seq, attempt)
            verified = self._implementation_evidence(
                evidence, row, capability, receipt
            )
            evidence_json = _dump(verified)
            if row["state"] in ("verified", "prepared", "landed"):
                if (
                    row["capability"] != capability
                    or row["receipt"] != receipt
                    or row["evidence_json"] != evidence_json
                ):
                    raise Conflict("implementation attempt already has different verified evidence")
                return self._implementation_attempt_document(db, row)
            if row["state"] != "reserved":
                raise Conflict(f"implementation attempt is {row['state']}, not reserved")
            db.execute(
                "UPDATE implementation_attempts SET state = 'verified', "
                "capability = ?, receipt = ?, evidence_json = ?, verified_at = ? "
                "WHERE action_seq = ? AND attempt = ?",
                (capability, receipt, evidence_json, _now(), action_seq, attempt),
            )
            return self._implementation_attempt_document(
                db, self._attempt_row(db, action_seq, attempt)
            )

    def fail_implementation_attempt(self, action_seq, attempt, failure):
        if not isinstance(failure, str) or not failure.strip():
            raise StoreError("implementation failure must be non-empty text")
        failure = failure.strip()
        with self._write() as db:
            _session_row, _session, _linked, action = self._linked_attempt_context(
                db, action_seq
            )
            row = self._attempt_row(db, action_seq, attempt)
            if row["state"] == "failed":
                if row["failure"] != failure:
                    raise Conflict("implementation attempt failed for a different reason")
                return self._implementation_attempt_document(db, row)
            if row["state"] != "reserved":
                raise Conflict(f"implementation attempt is {row['state']}, not reserved")
            db.execute(
                "UPDATE implementation_attempts SET state = 'failed', failure = ?, "
                "failed_at = ? WHERE action_seq = ? AND attempt = ?",
                (failure, _now(), action_seq, attempt),
            )
            beat_row = self._beat_row(db, action["beat_n"])
            beat = json.loads(beat_row["body_json"])
            detail = {
                "error": failure,
                "owed": "retry attested implementation",
                "cause_seq": action_seq,
                "kind": "commit",
                "attempt": attempt,
            }
            changed, _revision, _state, _detail = self._save_beat(
                db, beat, "failed", detail
            )
            if changed:
                self._bump_render(db)
            return self._implementation_attempt_document(
                db, self._attempt_row(db, action_seq, attempt)
            )

    def _implementation_commit_plan(self, document, session, attempt):
        if not isinstance(document, dict) or set(document) != _COMMIT_PLAN_FIELDS:
            raise StoreError("implementation commit plan has an unsupported shape")
        plan = _copy(document)
        if type(plan["version"]) is not int or plan["version"] != 1:
            raise StoreError("implementation commit plan version must be 1")
        for name in ("commit", "parent", "tree"):
            if not isinstance(plan[name], str) or not _FULL_SHA.fullmatch(plan[name]):
                raise StoreError(f"implementation commit plan {name} is invalid")
        if not isinstance(plan["outputTree"], str) or not _SHA256.fullmatch(plan["outputTree"]):
            raise StoreError("implementation commit plan outputTree is invalid")
        if plan["parent"] != session["target"]["head_sha"]:
            raise Conflict("implementation commit parent is not the frozen target head")
        evidence = json.loads(attempt["evidence_json"])
        if plan["outputTree"] != evidence["outputTree"]:
            raise Conflict("implementation commit plan does not match verified output")
        if plan["branch"] != session["delivery_branch"]:
            raise Conflict("implementation commit plan names a different branch")
        return plan

    def prepare_implementation_land(self, action_seq, attempt, commit_plan):
        with self._write() as db:
            _session_row, session, _linked, _action = self._linked_attempt_context(
                db, action_seq
            )
            row = self._attempt_row(db, action_seq, attempt)
            if row["state"] not in ("verified", "prepared", "landed"):
                raise Conflict(f"implementation attempt is {row['state']}, not verified")
            if row["evidence_json"] is None:
                raise Conflict("implementation attempt has no verified evidence")
            plan = self._implementation_commit_plan(commit_plan, session, row)
            plan_json = _dump(plan)
            if row["state"] in ("prepared", "landed"):
                if row["commit_plan_json"] != plan_json:
                    raise Conflict("implementation attempt already has a different commit plan")
                return self._implementation_attempt_document(db, row)
            db.execute(
                "UPDATE implementation_attempts SET state = 'prepared', "
                "commit_plan_json = ?, prepared_at = ? "
                "WHERE action_seq = ? AND attempt = ?",
                (plan_json, _now(), action_seq, attempt),
            )
            return self._implementation_attempt_document(
                db, self._attempt_row(db, action_seq, attempt)
            )

    def finish_implementation_land(self, action_seq, attempt, commit, branch):
        if not isinstance(commit, str) or not _FULL_SHA.fullmatch(commit):
            raise StoreError("implementation commit must be a full lowercase SHA")
        if not isinstance(branch, str) or not branch.strip() or branch != branch.strip():
            raise StoreError("implementation branch must be non-empty text")
        with self._write() as db:
            _session_row, session, _linked, action = self._linked_attempt_context(
                db, action_seq
            )
            row = self._attempt_row(db, action_seq, attempt)
            if row["state"] not in ("prepared", "landed"):
                raise Conflict(f"implementation attempt is {row['state']}, not prepared")
            plan = json.loads(row["commit_plan_json"])
            if commit != plan["commit"] or branch != plan["branch"]:
                raise Conflict("implementation landing does not match its persisted plan")
            beat_row = self._beat_row(db, action["beat_n"])
            current = json.loads(beat_row["delivery_json"]) if beat_row["delivery_json"] else None
            entry = {
                "state": "landed",
                "what": json.loads(beat_row["body_json"]).get("claim", ""),
                "where": commit,
            }
            desired = {
                "artifact": commit,
                "branch": branch,
                "cause_seq": action_seq,
                "kind": "commit",
                "entry": entry,
                "attempt": attempt,
                "attestation": json.loads(row["evidence_json"]),
            }
            if row["state"] == "landed":
                if beat_row["delivery_state"] != "landed" or current != desired:
                    raise Conflict("landed implementation receipt no longer matches")
                if action["state"] == "applied":
                    db.execute(
                        "UPDATE actions SET state = 'acked', acked_at = ? WHERE seq = ?",
                        (_now(), action_seq),
                    )
                return self._delivery_receipt(
                    action_seq, action["beat_n"], "landed", current
                )
            beat = json.loads(beat_row["body_json"])
            if beat.get("state") != "accepted":
                raise Conflict("linked implementation beat is no longer accepted")
            beat["landed"] = commit
            beat["delivery_kind"] = "commit"
            beat["branch"] = branch
            beat_changed, _revision, _state, _detail = self._save_beat(
                db, beat, "landed", desired
            )
            lands = session.setdefault("lands", [])
            if not isinstance(lands, list):
                raise StoreError("session lands must be a list")
            session_changed = False
            if entry not in lands:
                lands.append(entry)
                session_changed = self._save_session(
                    db, session, allow_new_lands=True
                )
            db.execute(
                "UPDATE implementation_attempts SET state = 'landed', landed_at = ? "
                "WHERE action_seq = ? AND attempt = ?",
                (_now(), action_seq, attempt),
            )
            if action["state"] == "applied":
                db.execute(
                    "UPDATE actions SET state = 'acked', acked_at = ? WHERE seq = ?",
                    (_now(), action_seq),
                )
            if beat_changed or session_changed:
                self._bump_render(db)
            return self._delivery_receipt(
                action_seq, action["beat_n"], "landed", desired
            )

    def read_trusted_context(self):
        with self._session_lock():
            target = self.frozen_target()
            expected_digest = target.get("trusted_context_sha256")
            expected_size = target.get("trusted_context_bytes")
            if expected_digest is None or expected_size is None:
                raise Conflict("session has no frozen trusted context")
            path = self.root / "trusted-context.json"
            try:
                data = path.read_bytes()
            except FileNotFoundError as error:
                raise Conflict("frozen trusted context is missing") from error
            digest = hashlib.sha256(data).hexdigest()
            if len(data) != expected_size or digest != expected_digest:
                raise Conflict("frozen trusted context does not match")
            return self._trusted_context_document(data, target)

    def read_diff(self, max_bytes=1_000_000):
        """The frozen three-dot diff as a document, for the review side. Verification is
        read_verified_target_diff's; this adds the bound and the transport encoding so
        the agent never has to open the projection file itself."""
        max_bytes = _positive(max_bytes, "diff max_bytes")
        target, data = self.read_verified_target_diff()
        if len(data) > max_bytes:
            raise Conflict(
                f"frozen diff is {len(data)} bytes, above max_bytes {max_bytes}"
            )
        try:
            content, encoding = data.decode("utf-8"), "utf-8"
        except UnicodeError:
            content = base64.b64encode(data).decode("ascii")
            encoding = "base64"
        return {
            "bytes": len(data),
            "content": content,
            "encoding": encoding,
            "sha256": target["diff_sha256"],
        }

    def read_object_bundle(self):
        with self._session_lock():
            target = self.frozen_target()
            expected_digest = target.get("object_bundle_sha256")
            expected_size = target.get("object_bundle_bytes")
            if expected_digest is None or expected_size is None:
                raise Conflict("session has no frozen Git object bundle")
            if expected_size > MAX_OBJECT_BUNDLE_MEMORY_BYTES:
                raise Conflict(
                    "frozen Git object bundle is too large to read into memory"
                )
            path = self.root / "pr.bundle"
            try:
                data = _bounded_file_bytes(
                    path,
                    expected_size,
                    "frozen Git object bundle",
                )
            except FileNotFoundError as error:
                raise Conflict("frozen Git object bundle is missing") from error
            digest = hashlib.sha256(data).hexdigest()
            if len(data) != expected_size or digest != expected_digest:
                raise Conflict("frozen Git object bundle does not match")
            return data

    def copy_verified_object_bundle(self, destination):
        destination = Path(destination)
        with self._session_lock():
            target = self.frozen_target()
            expected_digest = target.get("object_bundle_sha256")
            expected_size = target.get("object_bundle_bytes")
            if expected_digest is None or expected_size is None:
                raise Conflict("session has no frozen Git object bundle")
            source = self.root / "pr.bundle"
            digest, size = hashlib.sha256(), 0
            created = False
            try:
                try:
                    incoming = source.open("rb")
                except FileNotFoundError as error:
                    raise Conflict("frozen Git object bundle is missing") from error
                try:
                    outgoing = destination.open("xb")
                except OSError as error:
                    incoming.close()
                    raise StoreError(
                        "verified Git object bundle destination cannot be created"
                    ) from error
                created = True
                with incoming, outgoing:
                    while True:
                        chunk = incoming.read(1024 * 1024)
                        if not chunk:
                            break
                        outgoing.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                if size != expected_size or digest.hexdigest() != expected_digest:
                    raise Conflict("frozen Git object bundle does not match")
            except Exception:
                if created:
                    try:
                        destination.unlink()
                    except FileNotFoundError:
                        pass
                raise
            return target

    def review_marker(self):
        with self._read() as db:
            row = self._session_row(db)
            session = json.loads(row["body_json"])
            if session.get("target") is None:
                raise Conflict("session has no frozen target")
            token = hashlib.sha256(row["session_id"].encode("utf-8")).hexdigest()[:32]
        return f"<!-- underwrite-review:{token} -->"

    def read_verified_target_diff(self):
        with self._session_lock():
            with self._read() as db:
                session = json.loads(self._session_row(db)["body_json"])
                target = session.get("target")
                if target is None:
                    raise Conflict("session has no frozen target")
            try:
                data = (self.root / "pr.diff").read_bytes()
            except FileNotFoundError as error:
                raise Conflict("frozen target projection pr.diff is missing") from error
            digest = hashlib.sha256(data).hexdigest()
            if len(data) != target["diff_bytes"] or digest != target["diff_sha256"]:
                raise Conflict("frozen target projection pr.diff does not match")
            return target, data

    def verify_target_files(self):
        with self._session_lock():
            target = self.frozen_target()
            path = self.root / "pr.diff"
            try:
                digest, size = _bounded_file_identity(
                    path, target["diff_bytes"], "frozen target projection pr.diff"
                )
            except FileNotFoundError as error:
                raise Conflict("frozen target projection pr.diff is missing") from error
            if size != target["diff_bytes"] or digest != target["diff_sha256"]:
                raise Conflict("frozen target projection pr.diff does not match")
            if "trusted_context_sha256" in target:
                path = self.root / "trusted-context.json"
                try:
                    digest, size = _bounded_file_identity(
                        path,
                        target["trusted_context_bytes"],
                        "frozen trusted context",
                    )
                except FileNotFoundError as error:
                    raise Conflict("frozen trusted context is missing") from error
                if (
                    size != target["trusted_context_bytes"]
                    or digest != target["trusted_context_sha256"]
                ):
                    raise Conflict("frozen trusted context does not match")
            if "object_bundle_sha256" in target:
                path = self.root / "pr.bundle"
                try:
                    digest, size = _bounded_file_identity(
                        path,
                        target["object_bundle_bytes"],
                        "frozen Git object bundle",
                    )
                except FileNotFoundError as error:
                    raise Conflict("frozen Git object bundle is missing") from error
                if (
                    size != target["object_bundle_bytes"]
                    or digest != target["object_bundle_sha256"]
                ):
                    raise Conflict("frozen Git object bundle does not match")
            return target

    def branch_position(self):
        with self._read() as db:
            session = json.loads(self._session_row(db)["body_json"])
            target = session.get("target")
            if target is None:
                raise Conflict("session has no frozen target")
            if self._expected_delivery_kind(session) != "commit":
                raise Conflict("session is not in branch delivery mode")
            deliveries = []
            for row in db.execute(
                "SELECT n, delivery_json FROM beats "
                "WHERE delivery_state = 'landed' ORDER BY n"
            ):
                detail = json.loads(row["delivery_json"])
                if detail.get("kind") != "commit":
                    raise Conflict(
                        f"beat {row['n']} has a non-commit delivery in branch mode"
                    )
                deliveries.append({"beat_n": row["n"], **detail})
        deliveries.sort(key=lambda item: item["cause_seq"])
        expected = deliveries[-1]["artifact"] if deliveries else target["head_sha"]
        return {
            "target": target,
            "delivery_branch": session.get("delivery_branch"),
            "expected_head": expected,
            "deliveries": deliveries,
        }

    def put_beat(self, document):
        beat = self._beat_document(document)
        with self._first_work_write() as db:
            session, _execution_policy = self._session_execution_policy(db)
            if session.get("linked_implementation") is not None:
                raise Conflict("linked implementation finding is immutable")
            row = db.execute(
                "SELECT body_json, delivery_state, delivery_json FROM beats WHERE n = ?",
                (beat["n"],),
            ).fetchone()
            if row is None:
                self._validate_new_beat(beat)
            if row is not None:
                current = json.loads(row["body_json"])
                for field in ("state", "call", "landed", "branch", "delivery_kind"):
                    if field in current:
                        beat[field] = current[field]
                    else:
                        beat.pop(field, None)
                if "resolution_kind" not in beat and "resolution_kind" in current:
                    beat["resolution_kind"] = current["resolution_kind"]
                if current.get("state") not in OPEN_STATES:
                    if "resolution_kind" in current:
                        beat["resolution_kind"] = current["resolution_kind"]
                    else:
                        beat.pop("resolution_kind", None)
                if current.get("state") == "accepted":
                    if self._audience_mode(session) == "report":
                        frozen_fields = ("tier", "claim", "where", "slots", "diff")
                        changed_fields = [
                            field
                            for field in frozen_fields
                            if beat.get(field, _MISSING) != current.get(field, _MISSING)
                        ]
                        if changed_fields:
                            raise Conflict(
                                f"accepted report beat {beat['n']} cannot change "
                                + ", ".join(changed_fields)
                            )
                    current_slots = current.get("slots")
                    incoming_slots = beat.get("slots")
                    if incoming_slots is not None and not isinstance(incoming_slots, dict):
                        raise StoreError(f"beat {beat['n']} slots must be an object")
                    if isinstance(current_slots, dict) and "fix" in current_slots:
                        slots = beat.setdefault("slots", {})
                        if not isinstance(slots, dict):
                            raise StoreError(f"beat {beat['n']} slots must be an object")
                        slots["fix"] = current_slots["fix"]
                    elif isinstance(incoming_slots, dict):
                        incoming_slots.pop("fix", None)
            changed, _revision, _state, _detail = self._save_beat(db, beat)
            if changed:
                self._bump_render(db)
        return next(item for item in self.snapshot()[1] if item["n"] == beat["n"])

    def snapshot(self):
        with self._read() as db:
            row = self._session_row(db)
            session = json.loads(row["body_json"])
            session["schema_version"] = row["format_version"]
            beats = [
                json.loads(item["body_json"])
                for item in db.execute("SELECT * FROM beats ORDER BY n")
            ]
        return session, beats

    def presentation_snapshot(self):
        with self._read() as db:
            session_row = self._session_row(db)
            session = json.loads(session_row["body_json"])
            session["schema_version"] = session_row["format_version"]
            beats = []
            for row in db.execute("SELECT * FROM beats ORDER BY n"):
                beat = json.loads(row["body_json"])
                delivery = (
                    json.loads(row["delivery_json"])
                    if row["delivery_json"]
                    else {}
                )
                delivery["state"] = row["delivery_state"]
                beat["delivery"] = delivery
                beats.append(beat)
        return session, beats

    # ---- action queue -------------------------------------------------

    def _application(self, result, session, beats):
        return {
            "result": _copy(result),
            "session": None if session is None else _copy(session),
            "beats": [_copy(beat) for beat in beats],
        }

    def _insert_action(
        self,
        db,
        seq,
        action_id,
        beat_n,
        kind,
        note,
        state,
        application,
        evidence="",
        produced_at=None,
        applied_at=None,
        acked_at=None,
    ):
        db.execute(
            "INSERT INTO actions "
            "(seq, action_id, beat_n, kind, note, state, result_json, evidence, "
            "produced_at, applied_at, acked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                seq,
                action_id,
                beat_n,
                kind,
                note,
                state,
                self._optional_dump(application),
                evidence,
                produced_at or _now(),
                applied_at,
                acked_at,
            ),
        )

    def _action(self, row):
        if row is None:
            return None
        application = json.loads(row["result_json"]) if row["result_json"] else None
        return {
            "seq": row["seq"],
            "action_id": row["action_id"],
            "n": row["beat_n"],
            "action": row["kind"],
            "note": row["note"],
            "state": row["state"],
            "result": None if application is None else application["result"],
        }

    def _head_row(self, db):
        return db.execute(
            "SELECT * FROM actions WHERE state NOT IN ('acked', 'abandoned') "
            "ORDER BY seq LIMIT 1"
        ).fetchone()

    def _require_head(self, db, seq):
        head = self._head_row(db)
        if head is None:
            raise Conflict("there is no action at the head")
        if head["seq"] != seq:
            raise Conflict(f"action {head['seq']} is at the head, not {seq}")
        return head

    def _handled_seq(self, db):
        head = self._head_row(db)
        if head is None:
            return db.execute("SELECT COALESCE(MAX(seq), 0) FROM actions").fetchone()[0]
        return db.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM actions WHERE seq < ?",
            (head["seq"],),
        ).fetchone()[0]

    def produce(self, action_id, n, kind, note, session_id=None):
        if not isinstance(action_id, str) or not action_id.strip():
            raise StoreError("action_id must be non-empty text")
        action_id = action_id.strip()
        if kind not in ACTIONS:
            raise StoreError(f"action must be one of {', '.join(ACTIONS)}")
        if n is not None:
            n = _positive(n, "beat n")
        if kind not in NAVIGATION and n is None:
            raise StoreError(f"{kind} requires a beat")
        if not isinstance(note, str):
            raise StoreError("note must be text")
        note = note.strip()
        if session_id is not None and (
            not isinstance(session_id, str) or not session_id.strip()
        ):
            raise StoreError("session_id must be non-empty text")

        with self._first_work_write() as db:
            if session_id is not None and self._session_row(db)["session_id"] != session_id:
                raise Conflict("session identity does not match")
            session, execution_policy = self._session_execution_policy(db)
            if session.get("linked_implementation") is not None:
                raise Conflict(
                    "linked implementation actions are reserved for the attested gateway"
                )
            existing = db.execute(
                "SELECT * FROM actions WHERE action_id = ?", (action_id,)
            ).fetchone()
            if existing is not None:
                if (existing["beat_n"], existing["kind"], existing["note"]) != (n, kind, note):
                    raise Conflict(f"action_id {action_id!r} names a different action")
                return self._action(existing)
            if kind == "accept" and execution_policy is not None:
                replacement_reason = self._replacement_reason(
                    session, execution_policy
                )
                if replacement_reason:
                    raise Conflict(
                        replacement_reason
                        + "; start a supervised replacement session"
                    )
                if (
                    execution_policy["mode"] == "no_exec"
                    and self._expected_delivery_kind(session) == "commit"
                ):
                    raise Conflict(
                        "branch implementation is disabled for untrusted PR snapshots"
                    )
            if kind == "decide" and not note:
                raise StoreError("decide requires a non-empty note")

            seq = db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM actions").fetchone()[0]
            if kind in NAVIGATION:
                self._insert_action(
                    db, seq, action_id, n, kind, note, "produced", None
                )
            else:
                head = self._head_row(db)
                if head is not None and head["state"] == "produced":
                    raise Conflict(
                        f"action {head['seq']} must be applied before {kind} can be recorded"
                    )
                row = self._beat_row(db, n)
                beat = json.loads(row["body_json"])
                delivery_state = row["delivery_state"]
                delivery = json.loads(row["delivery_json"]) if row["delivery_json"] else None
                if kind in RESOLVE:
                    resolution_kind = beat.get("resolution_kind")
                    if kind == "accept" and resolution_kind == "decision":
                        raise Conflict(
                            f"accept does not match beat {n} resolution_kind 'decision'"
                        )
                    if kind == "decide" and resolution_kind == "delivery":
                        raise Conflict(
                            f"decide does not match beat {n} resolution_kind 'delivery'"
                        )
                    if beat.get("state") not in RESOLVABLE[kind]:
                        raise Conflict(
                            f"beat {n} is {beat.get('state')}, which cannot become {RESOLVE[kind]}"
                        )
                    if (
                        kind == "decide"
                        and beat.get("state") == "accepted"
                        and delivery_state == "landed"
                    ):
                        raise Conflict(f"beat {n} has already landed")
                    if kind == "accept" and self._audience_mode(session) == "report":
                        accepted = _copy(beat)
                        accepted["state"] = "accepted"
                        if note:
                            accepted["call"] = note
                        problems = validate_beat(accepted, "report", final=True)
                        if problems:
                            raise Conflict(
                                "report acceptance requires a shippable beat: "
                                + "; ".join(problems)
                            )
                    beat["state"] = RESOLVE[kind]
                    if kind == "accept":
                        audience_mode = self._audience_mode(session)
                        delivery_kind = self._expected_delivery_kind(session)
                        if audience_mode == "report":
                            delivery_state, delivery = "none", None
                        elif delivery_kind is None:
                            raise Conflict(
                                "session has no branch, review, or report audience"
                            )
                        else:
                            delivery_state, delivery = "pending", {
                                "cause_seq": seq,
                                "kind": delivery_kind,
                            }
                    else:
                        delivery_state, delivery = "none", None
                if note:
                    beat["call"] = note
                changed, revision, delivery_state, delivery = self._save_beat(
                    db, beat, delivery_state, delivery
                )
                if changed:
                    self._bump_render(db)
                result = {
                    "kind": "beat",
                    "n": n,
                    "state": beat.get("state"),
                    "call": beat.get("call", ""),
                    "revision": revision,
                    "delivery": delivery_state,
                }
                application = self._application(result, None, (beat,))
                timestamp = _now()
                self._insert_action(
                    db,
                    seq,
                    action_id,
                    n,
                    kind,
                    note,
                    "applied",
                    application,
                    produced_at=timestamp,
                    applied_at=timestamp,
                )
            row = db.execute("SELECT * FROM actions WHERE seq = ?", (seq,)).fetchone()
            return self._action(row)

    def head(self):
        with self._read() as db:
            return self._action(self._head_row(db))

    def _application_input(
        self,
        result,
        session,
        beats,
        reference_session=_MISSING,
        authoritative=False,
    ):
        if not isinstance(result, dict):
            raise StoreError("result must be an absolute object")
        normalized_session = None
        if session is not None:
            candidate = _copy(session)
            if isinstance(reference_session, dict):
                stored_target = reference_session.get("target")
                if (
                    stored_target is not None
                    and candidate.get("target") == stored_target
                ):
                    candidate.pop("legacy_pr", None)
                    candidate.pop("execution_policy", None)
                    if "execution_policy" in reference_session:
                        candidate["execution_policy"] = _copy(
                            reference_session["execution_policy"]
                        )
                elif stored_target is None and candidate.get("target") is None:
                    supplied_marker = candidate.pop("legacy_pr", None)
                    candidate.pop("execution_policy", None)
                    identity = _legacy_pr_identity(candidate)
                    stored_marker = reference_session.get("legacy_pr")
                    if isinstance(stored_marker, dict) and (
                        authoritative or supplied_marker == stored_marker
                    ):
                        candidate["legacy_pr"] = _copy(stored_marker)
                    elif identity is not None:
                        candidate["legacy_pr"] = identity
            _version, normalized_session = self._session_document(candidate)
        normalized_beats = tuple(
            sorted(
                (self._canonical_beat(self._beat_document(beat)) for beat in beats),
                key=lambda beat: beat["n"],
            )
        )
        return (
            self._application(result, normalized_session, normalized_beats),
            normalized_session,
            normalized_beats,
        )

    def _same_application(self, row, application):
        current = json.loads(row["result_json"])
        for value in (current, application):
            value["beats"] = [
                self._canonical_beat(beat) if isinstance(beat, dict) else beat
                for beat in value.get("beats", [])
            ]
        current_session = current.get("session")
        incoming_session = application.get("session")
        if (
            isinstance(current_session, dict)
            and isinstance(incoming_session, dict)
            and "execution_policy" not in incoming_session
            and incoming_session.get("target") == current_session.get("target")
            and isinstance(current_session.get("execution_policy"), dict)
            and current_session["execution_policy"].get("mode") == "no_exec"
        ):
            incoming_session["execution_policy"] = _copy(
                current_session["execution_policy"]
            )
        return current == application

    def apply(self, seq, result, session=None, beats=()):
        _positive(seq, "seq")
        with self._write() as db:
            row = db.execute("SELECT * FROM actions WHERE seq = ?", (seq,)).fetchone()
            if row is None:
                raise StoreError(f"no action {seq}")
            if row["state"] in ("applied", "acked"):
                stored = json.loads(row["result_json"])
                application, _normalized_session, _normalized_beats = (
                    self._application_input(
                        result,
                        session,
                        beats,
                        reference_session=stored.get("session"),
                    )
                )
                if not self._same_application(row, application):
                    raise Conflict(f"action {seq} already has a different application")
                return self._action(row)
            if row["state"] == "abandoned":
                raise Conflict(f"action {seq} was abandoned")
            self._require_head(db, seq)
            if row["kind"] not in NAVIGATION:
                raise Conflict(f"action {seq} must be reconciled, not applied")
            current_session = json.loads(self._session_row(db)["body_json"])
            application, normalized_session, normalized_beats = self._application_input(
                result,
                session,
                beats,
                reference_session=current_session,
                authoritative=True,
            )
            if normalized_session is None:
                raise StoreError("navigation apply requires the absolute session document")

            changed = False
            if normalized_session is not None:
                changed = self._save_session(db, normalized_session) or changed
            for beat in normalized_beats:
                current = db.execute(
                    "SELECT body_json FROM beats WHERE n = ?", (beat["n"],)
                ).fetchone()
                if current is not None:
                    if self._canonical_beat(json.loads(current["body_json"])) != beat:
                        raise Conflict(
                            f"navigation cannot replace existing beat {beat['n']}"
                        )
                else:
                    self._validate_new_beat(beat)
                    beat_changed, _revision, _state, _detail = self._save_beat(db, beat)
                    changed = beat_changed or changed
            if changed:
                self._bump_render(db)
            timestamp = _now()
            db.execute(
                "UPDATE actions SET state = 'applied', result_json = ?, applied_at = ? "
                "WHERE seq = ?",
                (_dump(application), timestamp, seq),
            )
            updated = db.execute("SELECT * FROM actions WHERE seq = ?", (seq,)).fetchone()
            return self._action(updated)

    def ack(self, seq):
        _non_negative(seq, "seq")
        with self._write() as db:
            row = db.execute("SELECT * FROM actions WHERE seq = ?", (seq,)).fetchone()
            if row is None:
                raise StoreError(f"no action {seq}")
            if row["state"] in ("acked", "abandoned"):
                return {"handled_seq": self._handled_seq(db)}
            self._require_head(db, seq)
            if row["state"] != "applied":
                raise Conflict(f"action {seq} has not been applied")
            if row["kind"] == "accept":
                beat = self._beat_row(db, row["beat_n"])
                beat_state = json.loads(beat["body_json"]).get("state")
                delivery = beat["delivery_state"]
                detail = json.loads(beat["delivery_json"]) if beat["delivery_json"] else {}
                session = json.loads(self._session_row(db)["body_json"])
                audience_mode = self._audience_mode(session)
                superseded = db.execute(
                    "SELECT EXISTS(SELECT 1 FROM actions WHERE seq > ? AND beat_n = ? "
                    "AND kind = 'decide' AND state IN ('applied', 'acked'))",
                    (seq, row["beat_n"]),
                ).fetchone()[0]
                can_ack_without_landing = (
                    (audience_mode == "review" and detail.get("kind") == "review")
                    or (
                        audience_mode == "report"
                        and delivery == "none"
                        and not detail
                    )
                )
                if beat_state != "accepted" and not superseded:
                    raise Conflict(
                        f"accepted action {seq} no longer resolves to an accepted beat"
                    )
                if not can_ack_without_landing and delivery != "landed" and not superseded:
                    raise Conflict(f"accepted action {seq} has not landed")
            db.execute(
                "UPDATE actions SET state = 'acked', acked_at = ? WHERE seq = ?",
                (_now(), seq),
            )
            return {"handled_seq": self._handled_seq(db)}

    # ---- delivery and recovery ---------------------------------------

    def _action_row(self, db, seq):
        row = db.execute("SELECT * FROM actions WHERE seq = ?", (seq,)).fetchone()
        if row is None:
            raise StoreError(f"no action {seq}")
        return row

    def _delivery_receipt(self, seq, beat_n, state, detail):
        return {"seq": seq, "beat_n": beat_n, "state": state, **_copy(detail)}

    def land(self, seq, beat_n, artifact, kind, branch=None, land_entry=None):
        _positive(seq, "seq")
        _positive(beat_n, "beat n")
        if not isinstance(artifact, str) or not artifact.strip():
            raise StoreError("artifact must be non-empty text")
        artifact = artifact.strip()
        if kind not in ("commit", "review"):
            raise StoreError("delivery kind must be commit or review")
        if branch is not None and not isinstance(branch, str):
            raise StoreError("branch must be text")
        branch = branch.strip() if branch else None
        if kind == "commit" and branch is None:
            raise StoreError("commit delivery requires a branch")
        if kind == "review" and branch is not None:
            raise StoreError("review delivery cannot name a branch")
        if land_entry is not None and not isinstance(land_entry, dict):
            raise StoreError("land_entry must be an object")

        with self._write() as db:
            action = self._action_row(db, seq)
            if action["kind"] != "accept" or action["beat_n"] != beat_n:
                raise Conflict(f"action {seq} is not the accept for beat {beat_n}")
            if action["state"] not in ("applied", "acked"):
                raise Conflict(f"accept action {seq} is {action['state']}, not applied")
            if action["state"] == "applied":
                self._require_head(db, seq)
            row = self._beat_row(db, beat_n)
            beat = json.loads(row["body_json"])
            current = json.loads(row["delivery_json"]) if row["delivery_json"] else None
            session, execution_policy = self._session_execution_policy(db)
            expected_kind = self._expected_delivery_kind(session)
            if expected_kind != kind:
                raise Conflict(
                    f"{kind} delivery does not match {session.get('audience')!r}"
                )
            if (
                execution_policy is not None
                and execution_policy.get("mode") == "gateway_attested"
            ):
                raise Conflict(
                    "gateway-attested commit delivery must use the implementation gateway"
                )
            if current and current.get("kind") not in (None, kind):
                raise Conflict(
                    f"beat {beat_n} expects {current.get('kind')} delivery, not {kind}"
                )
            default_entry = {
                "state": "landed",
                "what": beat.get("claim", ""),
                "where": artifact,
            }
            if land_entry is not None:
                entry = _copy(land_entry)
            elif row["delivery_state"] == "landed" and current and current.get("entry"):
                entry = current["entry"]
            else:
                entry = default_entry
            desired = {
                "artifact": artifact,
                "branch": branch,
                "cause_seq": seq,
                "kind": kind,
                "entry": entry,
            }
            if row["delivery_state"] == "landed":
                if current != desired:
                    raise Conflict(f"beat {beat_n} already landed as {current.get('artifact')}")
                return self._delivery_receipt(seq, beat_n, "landed", current)
            replacement_reason = self._replacement_reason(session, execution_policy)
            if replacement_reason:
                raise Conflict(
                    replacement_reason + "; start a supervised replacement session"
                )
            if (
                kind == "commit"
                and execution_policy is not None
                and execution_policy["mode"] == "no_exec"
            ):
                raise Conflict(
                    "commit delivery is disabled for untrusted PR snapshots"
                )
            if beat.get("state") != "accepted":
                raise Conflict(f"beat {beat_n} is {beat.get('state')}, not accepted")

            beat["landed"] = artifact
            beat["delivery_kind"] = kind
            if branch:
                beat["branch"] = branch
            else:
                beat.pop("branch", None)
            beat_changed, _revision, _state, _detail = self._save_beat(
                db, beat, "landed", desired
            )

            session_row = self._session_row(db)
            session = json.loads(session_row["body_json"])
            lands = session.setdefault("lands", [])
            if not isinstance(lands, list):
                raise StoreError("session lands must be a list")
            session_changed = False
            if entry not in lands:
                lands.append(entry)
                session_changed = self._save_session(
                    db, session, allow_new_lands=True
                )
            if beat_changed or session_changed:
                self._bump_render(db)
            return self._delivery_receipt(seq, beat_n, "landed", desired)

    def fail(self, seq, error, owed):
        _positive(seq, "seq")
        if not isinstance(error, str) or not error.strip():
            raise StoreError("error must be non-empty text")
        if not isinstance(owed, str) or not owed.strip():
            raise StoreError("owed must be non-empty text")
        error, owed = error.strip(), owed.strip()
        with self._write() as db:
            action = self._action_row(db, seq)
            if action["kind"] != "accept" or action["beat_n"] is None:
                raise Conflict(f"action {seq} is not an accept")
            if action["state"] not in ("applied", "acked"):
                raise Conflict(f"accept action {seq} is {action['state']}, not applied")
            if action["state"] == "applied":
                self._require_head(db, seq)
            beat_n = action["beat_n"]
            row = self._beat_row(db, beat_n)
            beat = json.loads(row["body_json"])
            current = json.loads(row["delivery_json"]) if row["delivery_json"] else None
            session = json.loads(self._session_row(db)["body_json"])
            if session.get("linked_implementation") is not None:
                raise Conflict(
                    "linked implementation failure must use the implementation gateway"
                )
            if self._audience_mode(session) == "report":
                raise Conflict("report inclusion has no external delivery to fail")
            desired = {
                "error": error,
                "owed": owed,
                "cause_seq": seq,
                "kind": None if current is None else current.get("kind"),
            }
            if row["delivery_state"] == "failed":
                if current == desired:
                    return self._delivery_receipt(seq, beat_n, "failed", current)
            if row["delivery_state"] == "landed":
                raise Conflict(f"beat {beat_n} has already landed")
            if beat.get("state") != "accepted":
                raise Conflict(f"beat {beat_n} is {beat.get('state')}, not accepted")
            changed, _revision, _state, _detail = self._save_beat(
                db, beat, "failed", desired
            )
            if changed:
                self._bump_render(db)
            return self._delivery_receipt(seq, beat_n, "failed", desired)

    def delivery_state(self):
        with self._read() as db:
            produced = db.execute("SELECT COALESCE(MAX(seq), 0) FROM actions").fetchone()[0]
            handled = self._handled_seq(db)
            head = self._head_row(db)
            recovery = db.execute(
                "SELECT EXISTS(SELECT 1 FROM actions WHERE state NOT IN ('acked','abandoned')) "
                "OR EXISTS(SELECT 1 FROM beats WHERE delivery_state IN ('pending','failed'))"
            ).fetchone()[0]
            session_row = self._session_row(db)
            revision = session_row["render_revision"]
            session_id = session_row["session_id"]
        return {
            "session_id": session_id,
            "seq": produced,
            "handled_seq": handled,
            "head_id": None if head is None else head["action_id"],
            "head_kind": None if head is None else head["kind"],
            "head_state": None if head is None else head["state"],
            "recovery": bool(recovery),
            "render_revision": revision,
        }

    def reconcile(self):
        with self._read() as db:
            head = self._action(self._head_row(db))
            session, execution_policy = self._session_execution_policy(db)
            replacement_reason = self._replacement_reason(
                session, execution_policy
            )
            block_commit_delivery = (
                execution_policy is not None
                and execution_policy["mode"] == "no_exec"
                and (
                    session.get("target", {}).get("kind") == "github_pr"
                    or isinstance(session.get("legacy_pr"), dict)
                )
            )
            pending, failed = [], []
            for row in db.execute(
                "SELECT n, delivery_state, delivery_json FROM beats "
                "WHERE delivery_state IN ('pending', 'failed') ORDER BY n"
            ):
                item = {
                    "beat_n": row["n"],
                    **(json.loads(row["delivery_json"]) if row["delivery_json"] else {}),
                }
                if replacement_reason:
                    item["blocked"] = True
                    item["blocked_reason"] = (
                        replacement_reason + BLOCKED_REPLACEMENT_DELIVERY_SUFFIX
                    )
                elif block_commit_delivery and item.get("kind") == "commit":
                    item["blocked"] = True
                    item["blocked_reason"] = BLOCKED_COMMIT_DELIVERY_REASON
                (pending if row["delivery_state"] == "pending" else failed).append(item)
            produced = db.execute("SELECT COALESCE(MAX(seq), 0) FROM actions").fetchone()[0]
            handled = self._handled_seq(db)
            revision = self._session_row(db)["render_revision"]
        return {
            "seq": produced,
            "handled_seq": handled,
            "head": head,
            "pending_deliveries": pending,
            "failed_deliveries": failed,
            "render_revision": revision,
            "recovery": bool(head or pending or failed),
        }

    def reconcile_action(self, seq, result, session=None, beats=(), evidence=""):
        _positive(seq, "seq")
        if not isinstance(evidence, str) or not evidence.strip():
            raise StoreError("evidence must be non-empty text")
        with self._write() as db:
            row = self._action_row(db, seq)
            if row["state"] in ("applied", "acked"):
                stored = json.loads(row["result_json"])
                application, _normalized_session, _normalized_beats = (
                    self._application_input(
                        result,
                        session,
                        beats,
                        reference_session=stored.get("session"),
                    )
                )
                if not self._same_application(row, application):
                    raise Conflict(f"action {seq} already has a different application")
                return self._action(row)
            if row["state"] == "abandoned":
                raise Conflict(f"action {seq} was abandoned")
            self._require_head(db, seq)
            current_session = json.loads(self._session_row(db)["body_json"])
            application, normalized_session, normalized_beats = self._application_input(
                result,
                session,
                beats,
                reference_session=current_session,
                authoritative=True,
            )
            if normalized_session is None and not normalized_beats:
                raise StoreError("reconcile requires an observed session or beat document")
            if normalized_session is not None:
                current = json.loads(self._session_row(db)["body_json"])
                if current != normalized_session:
                    raise Conflict("observed session does not match the stored session")
            for beat in normalized_beats:
                current = json.loads(self._beat_row(db, beat["n"])["body_json"])
                if self._canonical_beat(current) != beat:
                    raise Conflict(f"observed beat {beat['n']} does not match the stored beat")
            timestamp = _now()
            db.execute(
                "UPDATE actions SET state = 'applied', result_json = ?, evidence = ?, "
                "applied_at = ? WHERE seq = ?",
                (_dump(application), evidence.strip(), timestamp, seq),
            )
            updated = db.execute("SELECT * FROM actions WHERE seq = ?", (seq,)).fetchone()
            return self._action(updated)

    def abandon_head(self, seq, actor, reason):
        _positive(seq, "seq")
        if not isinstance(actor, str) or not actor.strip():
            raise StoreError("actor must be non-empty text")
        if not isinstance(reason, str) or not reason.strip():
            raise StoreError("reason must be non-empty text")
        actor, reason = actor.strip(), reason.strip()
        with self._write() as db:
            row = self._action_row(db, seq)
            if row["state"] == "abandoned":
                if (row["abandoned_by"], row["abandoned_reason"]) != (actor, reason):
                    raise Conflict(f"action {seq} was abandoned for a different reason")
                return self._action(row)
            if row["state"] == "acked":
                raise Conflict(f"action {seq} is already acknowledged")
            self._require_head(db, seq)
            if row["state"] != "produced":
                raise Conflict(f"action {seq} is already applied and cannot be abandoned")
            db.execute(
                "UPDATE actions SET state = 'abandoned', abandoned_at = ?, "
                "abandoned_by = ?, abandoned_reason = ? WHERE seq = ?",
                (_now(), actor, reason, seq),
            )
            updated = db.execute("SELECT * FROM actions WHERE seq = ?", (seq,)).fetchone()
            return self._action(updated)

    # ---- compatibility export ----------------------------------------

    def export_json(self):
        with self._session_lock():
            with self._read() as db:
                session_row = self._session_row(db)
                session = json.loads(session_row["body_json"])
                session["schema_version"] = session_row["format_version"]
                beats = [
                    json.loads(row["body_json"])
                    for row in db.execute("SELECT * FROM beats ORDER BY n")
                ]
                actions = list(db.execute("SELECT * FROM actions ORDER BY seq"))
                handled = self._handled_seq(db)

                beat_dir = self.root / "beats"
                beat_dir.mkdir(parents=True, exist_ok=True)
                expected = set()
                for beat in beats:
                    path = beat_dir / f"{beat['n']:02d}.json"
                    expected.add(path.name)
                    payload = json.dumps(beat, indent=2, ensure_ascii=False) + "\n"
                    _atomic_write(path, payload.encode("utf-8"))
                for path in beat_dir.glob("*.json"):
                    if path.name not in expected:
                        path.unlink()
                _fsync_directory(beat_dir)

                records = "".join(
                    json.dumps(
                        {
                            "seq": row["seq"],
                            "action_id": row["action_id"],
                            "n": row["beat_n"],
                            "action": row["kind"],
                            "note": row["note"],
                            "delivery_version": DELIVERY_VERSION,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ) + "\n"
                    for row in actions
                )
                _atomic_write(self.root / "decisions.jsonl", records.encode("utf-8"))
                _atomic_write(
                    self.root / "ack.json",
                    (
                        json.dumps(
                            {"version": DELIVERY_VERSION, "handled_seq": handled},
                            indent=2,
                        )
                        + "\n"
                    ).encode(),
                )
                session_payload = json.dumps(
                    session, indent=2, ensure_ascii=False
                ) + "\n"
                _atomic_write(
                    self.root / "session.json", session_payload.encode("utf-8")
                )
                findings = None
                if self._audience_mode(session) == "report":
                    findings = self.root / FINDINGS_FILE
                    _atomic_write(
                        findings, render_findings(session, beats).encode("utf-8")
                    )
        return {
            "session": str(self.root / "session.json"),
            "beats": len(beats),
            "seq": handled,
            "findings": str(findings) if findings else None,
        }
