#!/usr/bin/env python3
"""Transactional storage for one underwrite session."""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


DB_SCHEMA_VERSION = 3
SCHEMA_VERSION = 1
DELIVERY_VERSION = 1
ACTIONS = ("accept", "drop", "decide", "note", "next", "back", "skip")
NAVIGATION = ("next", "back", "skip")
RESOLVE = {"accept": "accepted", "drop": "dropped", "decide": "decided"}
RESOLVABLE = {"accept": ("flag",), "drop": ("flag",), "decide": ("flag", "accepted")}
OPEN_STATES = ("clean", "flag", "unverified")
RESOLUTION_KINDS = ("delivery", "decision")
_MISSING = object()
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TARGET_FIELDS = {
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


def _non_negative(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StoreError(f"{name} must be a non-negative integer")
    return value


def _positive(value, name):
    _non_negative(value, name)
    if value == 0:
        raise StoreError(f"{name} must be a positive integer")
    return value


def _fsync_directory(path):
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


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
            if version not in (1, 2):
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
            self._restore_failed_fix_intents(db)
            db.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")
            db.execute("COMMIT")
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

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
                "SELECT session_id, format_version FROM session WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise StoreError("session database has no session row")
            if not isinstance(row["session_id"], str) or not row["session_id"].strip():
                raise StoreError("session database has no session identity")
            if row["format_version"] != SCHEMA_VERSION:
                raise StoreError(
                    f"unsupported session format version {row['format_version']}"
                )
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
                version, body = self._session_document(session)
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
                for beat in beats:
                    state, delivery = self._delivery_from_document(
                        beat,
                        cause_seq=accept_seqs.get(beat["n"]),
                        kind=delivery_kind,
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
            if audience.get("mode") not in ("branch", "review"):
                raise StoreError("session audience mode must be branch or review")
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
        return version, body

    def _target_document(self, document):
        if not isinstance(document, dict):
            raise StoreError("session target must be an object")
        target = _copy(document)
        missing = sorted(_TARGET_FIELDS - set(target))
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
        return target

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

    def _expected_delivery_kind(self, session):
        audience = session.get("audience")
        if not isinstance(audience, dict):
            return None
        return {"branch": "commit", "review": "review"}.get(audience.get("mode"))

    def _delivery_from_document(self, beat, cause_seq=None, kind=None):
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
        current_mode = self._expected_delivery_kind(current)
        incoming_mode = self._expected_delivery_kind(body)
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

    def freeze_target(self, document, diff_source, metadata_source):
        target = _copy(document)
        for name in ("diff_sha256", "diff_bytes"):
            if name in target:
                raise StoreError(f"freeze-target computes {name}")

        staged = []
        with self._session_lock():
            try:
                diff, diff_sha256, diff_bytes = _stage_copy(
                    diff_source, self.root, "pr.diff"
                )
                staged.append(diff)
                metadata, _metadata_sha256, _metadata_bytes = _stage_copy(
                    metadata_source, self.root, "pr.json"
                )
                staged.append(metadata)
                target.update({
                    "diff_sha256": diff_sha256,
                    "diff_bytes": diff_bytes,
                })
                target = self._target_document(target)

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
                        if self._save_session(db, current, allow_new_target=True):
                            self._bump_render(db)
                    elif frozen != target:
                        raise Conflict("session target cannot change once frozen")
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
                digest, size = _file_identity(path)
            except FileNotFoundError as error:
                raise Conflict("frozen target projection pr.diff is missing") from error
            if size != target["diff_bytes"] or digest != target["diff_sha256"]:
                raise Conflict("frozen target projection pr.diff does not match")
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
        with self._write() as db:
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

        with self._write() as db:
            if session_id is not None and self._session_row(db)["session_id"] != session_id:
                raise Conflict("session identity does not match")
            existing = db.execute(
                "SELECT * FROM actions WHERE action_id = ?", (action_id,)
            ).fetchone()
            if existing is not None:
                if (existing["beat_n"], existing["kind"], existing["note"]) != (n, kind, note):
                    raise Conflict(f"action_id {action_id!r} names a different action")
                return self._action(existing)
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
                    beat["state"] = RESOLVE[kind]
                    if kind == "accept":
                        session = json.loads(self._session_row(db)["body_json"])
                        delivery_kind = self._expected_delivery_kind(session)
                        if delivery_kind is None:
                            raise Conflict("session has no branch or review audience")
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

    def _application_input(self, result, session, beats):
        if not isinstance(result, dict):
            raise StoreError("result must be an absolute object")
        normalized_session = None
        if session is not None:
            _version, normalized_session = self._session_document(session)
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
        return current == application

    def apply(self, seq, result, session=None, beats=()):
        _positive(seq, "seq")
        application, normalized_session, normalized_beats = self._application_input(
            result, session, beats
        )
        with self._write() as db:
            row = db.execute("SELECT * FROM actions WHERE seq = ?", (seq,)).fetchone()
            if row is None:
                raise StoreError(f"no action {seq}")
            if row["state"] in ("applied", "acked"):
                if not self._same_application(row, application):
                    raise Conflict(f"action {seq} already has a different application")
                return self._action(row)
            if row["state"] == "abandoned":
                raise Conflict(f"action {seq} was abandoned")
            self._require_head(db, seq)
            if row["kind"] not in NAVIGATION:
                raise Conflict(f"action {seq} must be reconciled, not applied")
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
                delivery = beat["delivery_state"]
                detail = json.loads(beat["delivery_json"]) if beat["delivery_json"] else {}
                superseded = db.execute(
                    "SELECT EXISTS(SELECT 1 FROM actions WHERE seq > ? AND beat_n = ? "
                    "AND kind = 'decide' AND state IN ('applied', 'acked'))",
                    (seq, row["beat_n"]),
                ).fetchone()[0]
                if detail.get("kind") != "review" and delivery != "landed" and not superseded:
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
            session = json.loads(self._session_row(db)["body_json"])
            expected_kind = self._expected_delivery_kind(session)
            if expected_kind != kind:
                raise Conflict(
                    f"{kind} delivery does not match {session.get('audience')!r}"
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
            "recovery": bool(recovery),
            "render_revision": revision,
        }

    def reconcile(self):
        with self._read() as db:
            head = self._action(self._head_row(db))
            pending, failed = [], []
            for row in db.execute(
                "SELECT n, delivery_state, delivery_json FROM beats "
                "WHERE delivery_state IN ('pending', 'failed') ORDER BY n"
            ):
                item = {
                    "beat_n": row["n"],
                    **(json.loads(row["delivery_json"]) if row["delivery_json"] else {}),
                }
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
        application, normalized_session, normalized_beats = self._application_input(
            result, session, beats
        )
        with self._write() as db:
            row = self._action_row(db, seq)
            if row["state"] in ("applied", "acked"):
                if not self._same_application(row, application):
                    raise Conflict(f"action {seq} already has a different application")
                return self._action(row)
            if row["state"] == "abandoned":
                raise Conflict(f"action {seq} was abandoned")
            self._require_head(db, seq)
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
        return {"session": str(self.root / "session.json"), "beats": len(beats), "seq": handled}
