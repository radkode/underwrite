"""Host-owned artifact storage and replay protection for execution evidence."""

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path


class StoreError(RuntimeError):
    """The gateway store is unavailable, corrupt, or used inconsistently."""


class ReplayConflict(StoreError):
    """An execution identity was already consumed or left ambiguous."""


@dataclass(frozen=True)
class ArtifactRef:
    sha256: str
    bytes: int

    def as_dict(self):
        return {"sha256": self.sha256, "bytes": self.bytes}


@dataclass(frozen=True)
class StoredExecution:
    request: bytes
    capability: bytes
    receipt: bytes
    artifacts: tuple
    validation: bytes

    def artifact(self, name):
        for artifact_name, reference in self.artifacts:
            if artifact_name == name:
                return reference
        raise KeyError(name)


def _canonical_bytes(value):
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise StoreError("gateway metadata is not canonical JSON") from error


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _read_flags():
    if not hasattr(os, "O_NOFOLLOW"):
        raise StoreError("gateway store requires no-follow file opens")
    return (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _reference(value, label):
    if not isinstance(value, dict) or set(value) != {"sha256", "bytes"}:
        raise StoreError(f"stored {label} reference is malformed")
    digest, size = value["sha256"], value["bytes"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
    ):
        raise StoreError(f"stored {label} reference is malformed")
    return ArtifactRef(digest, size)


class ContentStore:
    """Publish immutable bytes before SQLite makes them authoritative."""

    def __init__(self, root):
        self.root = Path(root)
        if not self.root.is_absolute():
            raise StoreError("gateway store root must be absolute")
        self.objects = self.root / "objects" / "sha256"
        self.staging = self.root / "staging"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.objects.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.staging.mkdir(mode=0o700, parents=True, exist_ok=True)
        for path in (self.root, self.objects, self.staging):
            if stat.S_IMODE(path.stat().st_mode) & 0o077:
                raise StoreError(f"gateway store path is not private: {path}")

    def _path(self, digest):
        return self.objects / digest[:2] / digest[2:]

    def put_bytes(self, data):
        if not isinstance(data, bytes):
            raise StoreError("artifact must be bytes")
        with tempfile.NamedTemporaryFile(
            dir=str(self.staging), prefix="artifact-", delete=False
        ) as handle:
            temporary = Path(handle.name)
            os.chmod(handle.name, 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            return self._publish(temporary, _sha256(data), len(data))
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def put_file(self, source, maximum_bytes=None):
        source = Path(source)
        if (
            maximum_bytes is not None
            and (
                isinstance(maximum_bytes, bool)
                or not isinstance(maximum_bytes, int)
                or maximum_bytes < 0
            )
        ):
            raise StoreError("artifact byte limit must be a non-negative integer")
        digest, size = hashlib.sha256(), 0
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=str(self.staging), prefix="artifact-", delete=False
            ) as output:
                temporary = Path(output.name)
                os.chmod(output.name, 0o600)
                try:
                    descriptor = os.open(str(source), _read_flags())
                except OSError as error:
                    raise StoreError(
                        f"artifact cannot be opened: {source}"
                    ) from error
                with os.fdopen(descriptor, "rb") as incoming:
                    before = os.fstat(incoming.fileno())
                    if not stat.S_ISREG(before.st_mode):
                        raise StoreError("artifact source must be a regular file")
                    while True:
                        chunk = incoming.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if maximum_bytes is not None and size > maximum_bytes:
                            raise StoreError(
                                "artifact exceeds its configured byte limit"
                            )
                        digest.update(chunk)
                        output.write(chunk)
                    after = os.fstat(incoming.fileno())
                    identity = lambda value: (
                        value.st_dev,
                        value.st_ino,
                        value.st_mode,
                        value.st_size,
                        value.st_mtime_ns,
                        value.st_ctime_ns,
                    )
                    if identity(before) != identity(after) or size != before.st_size:
                        raise StoreError("artifact changed while it was copied")
                output.flush()
                os.fsync(output.fileno())
            return self._publish(temporary, digest.hexdigest(), size)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def load_file(self, source, maximum_bytes):
        if (
            isinstance(maximum_bytes, bool)
            or not isinstance(maximum_bytes, int)
            or maximum_bytes < 0
        ):
            raise StoreError("artifact byte limit must be a non-negative integer")
        source = Path(source)
        try:
            descriptor = os.open(str(source), _read_flags())
        except OSError as error:
            raise StoreError(f"artifact cannot be opened: {source}") from error
        chunks = []
        size = 0
        with os.fdopen(descriptor, "rb") as incoming:
            before = os.fstat(incoming.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise StoreError("artifact source must be a regular file")
            while True:
                chunk = incoming.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > maximum_bytes:
                    raise StoreError("artifact exceeds its configured byte limit")
                chunks.append(chunk)
            after = os.fstat(incoming.fileno())
        identity = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
        if identity(before) != identity(after) or size != before.st_size:
            raise StoreError("artifact changed while it was read")
        return b"".join(chunks)

    def _publish(self, temporary, digest, size):
        destination = self._path(digest)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if stat.S_IMODE(destination.parent.stat().st_mode) & 0o077:
            raise StoreError(f"gateway store path is not private: {destination.parent}")
        try:
            os.link(str(temporary), str(destination))
        except FileExistsError:
            pass
        try:
            with destination.open("rb") as handle:
                os.fsync(handle.fileno())
            for path in (destination.parent, self.objects):
                directory = os.open(str(path), os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except OSError as error:
            raise StoreError("artifact could not be made durable") from error
        reference = ArtifactRef(digest, size)
        self.read(reference)
        return reference

    def read(self, reference):
        if not isinstance(reference, ArtifactRef):
            raise StoreError("artifact reference is invalid")
        _reference(reference.as_dict(), "artifact")
        path = self._path(reference.sha256)
        digest, size = hashlib.sha256(), 0
        try:
            descriptor = os.open(str(path), _read_flags())
        except OSError as error:
            raise StoreError("authoritative artifact is missing") from error
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise StoreError("authoritative artifact is not a regular file")
            chunks = []
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                digest.update(chunk)
                size += len(chunk)
            after = os.fstat(handle.fileno())
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_mode != after.st_mode
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
            or size != reference.bytes
            or digest.hexdigest() != reference.sha256
        ):
            raise StoreError("authoritative artifact does not match its reference")
        return b"".join(chunks)


class ReplayLedger:
    """Consume each replay identity once and persist one terminal result."""

    def __init__(self, content):
        if not isinstance(content, ContentStore):
            raise StoreError("replay ledger requires a ContentStore")
        self.content = content
        self.path = content.root / "gateway.sqlite3"
        self._initialize()

    def _connect(self):
        database = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA foreign_keys = ON")
        database.execute("PRAGMA synchronous = FULL")
        return database

    def _initialize(self):
        with self._connect() as database:
            database.execute("PRAGMA journal_mode = WAL")
            database.execute("BEGIN IMMEDIATE")
            try:
                database.execute(
                    """
                    CREATE TABLE IF NOT EXISTS attempts (
                        replay_key TEXT PRIMARY KEY,
                        challenge_key TEXT NOT NULL UNIQUE,
                        request_sha256 TEXT NOT NULL,
                        request BLOB NOT NULL,
                        state TEXT NOT NULL CHECK (
                            state IN ('preparing', 'ready', 'executing', 'failed', 'complete')
                        ),
                        source_json BLOB NOT NULL,
                        capability BLOB,
                        capability_sha256 TEXT,
                        receipt BLOB,
                        receipt_sha256 TEXT,
                        artifacts_json BLOB,
                        artifacts_sha256 TEXT,
                        validation_json BLOB,
                        validation_sha256 TEXT,
                        failure TEXT,
                        CHECK (
                            (capability IS NULL AND capability_sha256 IS NULL)
                            OR (
                                capability IS NOT NULL
                                AND capability_sha256 IS NOT NULL
                            )
                        ),
                        CHECK (
                            state IN ('preparing', 'failed')
                            OR capability IS NOT NULL
                        ),
                        CHECK (
                            state != 'preparing' OR capability IS NULL
                        ),
                        CHECK (
                            (
                                state = 'complete'
                                AND receipt IS NOT NULL
                                AND receipt_sha256 IS NOT NULL
                                AND artifacts_json IS NOT NULL
                                AND artifacts_sha256 IS NOT NULL
                                AND validation_json IS NOT NULL
                                AND validation_sha256 IS NOT NULL
                            )
                            OR (
                                state != 'complete'
                                AND receipt IS NULL
                                AND receipt_sha256 IS NULL
                                AND artifacts_json IS NULL
                                AND artifacts_sha256 IS NULL
                                AND validation_json IS NULL
                                AND validation_sha256 IS NULL
                            )
                        )
                    )
                    """
                )
                database.execute("COMMIT")
            except Exception:
                database.execute("ROLLBACK")
                raise
        os.chmod(self.path, 0o600)

    def reserve(self, replay_key, challenge_key, request, source):
        self._text(replay_key, "replay key")
        self._text(challenge_key, "challenge key")
        if not isinstance(request, bytes):
            raise StoreError("replay request must be bytes")
        if not isinstance(source, ArtifactRef):
            raise StoreError("source artifact reference is invalid")
        request_digest = _sha256(request)
        source_json = _canonical_bytes(source.as_dict())
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            try:
                row = database.execute(
                    "SELECT * FROM attempts WHERE replay_key = ? OR challenge_key = ?",
                    (replay_key, challenge_key),
                ).fetchone()
                if row is not None:
                    result = self._existing(
                        row,
                        replay_key,
                        challenge_key,
                        request,
                        request_digest,
                        source_json,
                    )
                    database.execute("COMMIT")
                    return result
                database.execute(
                    """
                    INSERT INTO attempts (
                        replay_key, challenge_key, request_sha256, request,
                        state, source_json
                    ) VALUES (?, ?, ?, ?, 'preparing', ?)
                    """,
                    (
                        replay_key,
                        challenge_key,
                        request_digest,
                        request,
                        source_json,
                    ),
                )
                database.execute("COMMIT")
                return None
            except Exception:
                database.execute("ROLLBACK")
                raise

    def _existing(
        self,
        row,
        replay_key,
        challenge_key,
        request,
        request_digest,
        source_json,
    ):
        if (
            row["replay_key"] != replay_key
            or row["challenge_key"] != challenge_key
            or row["request_sha256"] != request_digest
            or row["request"] != request
            or row["source_json"] != source_json
        ):
            raise ReplayConflict("execution challenge or replay identity was reused")
        if row["state"] == "complete":
            return self._stored(row)
        raise ReplayConflict(
            f"execution replay identity is already {row['state']} and cannot run again"
        )

    def lookup_complete(self, replay_key, request, source):
        """Return an exact completed replay before any new work is performed."""
        self._text(replay_key, "replay key")
        if not isinstance(request, bytes):
            raise StoreError("replay request must be bytes")
        if not isinstance(source, ArtifactRef):
            raise StoreError("source artifact reference is invalid")
        request_digest = _sha256(request)
        source_json = _canonical_bytes(source.as_dict())
        with self._connect() as database:
            row = database.execute(
                "SELECT * FROM attempts WHERE replay_key = ?", (replay_key,)
            ).fetchone()
        if row is None:
            return None
        if (
            row["request_sha256"] != request_digest
            or row["request"] != request
            or row["source_json"] != source_json
        ):
            raise ReplayConflict("execution replay identity was reused")
        if row["state"] != "complete":
            raise ReplayConflict(
                f"execution replay identity is already {row['state']} "
                "and cannot run again"
            )
        return self._stored(row)

    def record_capability(self, replay_key, capability):
        if not isinstance(capability, bytes) or not capability:
            raise StoreError("capability must be non-empty bytes")
        self._transition(
            replay_key,
            "preparing",
            "ready",
            "capability = ?, capability_sha256 = ?",
            (capability, _sha256(capability)),
        )

    def begin_execution(self, replay_key):
        self._transition(replay_key, "ready", "executing", "", ())

    def fail(self, replay_key, reason):
        self._text(reason, "failure reason")
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            try:
                row = database.execute(
                    "SELECT state FROM attempts WHERE replay_key = ?", (replay_key,)
                ).fetchone()
                if row is None:
                    raise StoreError("execution replay identity was not reserved")
                if row["state"] in ("failed", "complete"):
                    raise ReplayConflict(
                        f"execution is already terminal as {row['state']}"
                    )
                database.execute(
                    "UPDATE attempts SET state = 'failed', failure = ? WHERE replay_key = ?",
                    (reason, replay_key),
                )
                database.execute("COMMIT")
            except Exception:
                database.execute("ROLLBACK")
                raise

    def complete(self, replay_key, receipt, artifacts, validation):
        if not isinstance(receipt, bytes) or not receipt:
            raise StoreError("receipt must be non-empty bytes")
        if not isinstance(validation, bytes) or not validation:
            raise StoreError("validation result must be non-empty bytes")
        if not isinstance(artifacts, dict) or not artifacts:
            raise StoreError("completion artifacts must be a non-empty object")
        encoded = {}
        for name, reference in artifacts.items():
            self._text(name, "artifact name")
            if not isinstance(reference, ArtifactRef):
                raise StoreError("completion artifact reference is invalid")
            self.content.read(reference)
            encoded[name] = reference.as_dict()
        artifacts_json = _canonical_bytes(encoded)
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            try:
                row = database.execute(
                    "SELECT state FROM attempts WHERE replay_key = ?", (replay_key,)
                ).fetchone()
                if row is None:
                    raise StoreError("execution replay identity was not reserved")
                if row["state"] != "executing":
                    raise ReplayConflict(
                        f"execution cannot complete from state {row['state']}"
                    )
                database.execute(
                    """
                    UPDATE attempts
                    SET state = 'complete', receipt = ?, receipt_sha256 = ?,
                        artifacts_json = ?, artifacts_sha256 = ?,
                        validation_json = ?, validation_sha256 = ?, failure = NULL
                    WHERE replay_key = ?
                    """,
                    (
                        receipt,
                        _sha256(receipt),
                        artifacts_json,
                        _sha256(artifacts_json),
                        validation,
                        _sha256(validation),
                        replay_key,
                    ),
                )
                database.execute("COMMIT")
            except Exception:
                database.execute("ROLLBACK")
                raise
        return self.get(replay_key)

    def get(self, replay_key):
        self._text(replay_key, "replay key")
        with self._connect() as database:
            row = database.execute(
                "SELECT * FROM attempts WHERE replay_key = ?", (replay_key,)
            ).fetchone()
        if row is None:
            raise StoreError("execution replay identity was not found")
        if row["state"] != "complete":
            raise ReplayConflict(f"execution is {row['state']}, not complete")
        return self._stored(row)

    def _stored(self, row):
        raw_manifest = row["artifacts_json"]
        integrity_fields = (
            ("receipt", row["receipt"], row["receipt_sha256"]),
            ("artifact manifest", raw_manifest, row["artifacts_sha256"]),
            ("validation", row["validation_json"], row["validation_sha256"]),
        )
        for label, value, digest in integrity_fields:
            if (
                not isinstance(value, bytes)
                or not value
                or _sha256(value) != digest
            ):
                raise StoreError(f"stored {label} does not match its digest")
        try:
            values = json.loads(raw_manifest.decode("utf-8"))
        except (AttributeError, UnicodeError, json.JSONDecodeError) as error:
            raise StoreError("stored artifact manifest is malformed") from error
        if (
            not isinstance(values, dict)
            or not values
            or _canonical_bytes(values) != raw_manifest
        ):
            raise StoreError("stored artifact manifest is malformed")
        artifacts = []
        for name in sorted(values):
            self._text(name, "stored artifact name")
            reference = _reference(values[name], name)
            self.content.read(reference)
            artifacts.append((name, reference))
        capability = row["capability"]
        if (
            not isinstance(capability, bytes)
            or _sha256(capability) != row["capability_sha256"]
        ):
            raise StoreError("stored capability does not match its digest")
        request = row["request"]
        if (
            not isinstance(request, bytes)
            or _sha256(request) != row["request_sha256"]
        ):
            raise StoreError("stored request does not match its digest")
        return StoredExecution(
            request=request,
            capability=capability,
            receipt=row["receipt"],
            artifacts=tuple(artifacts),
            validation=row["validation_json"],
        )

    def _transition(self, replay_key, expected, state, assignment, values):
        self._text(replay_key, "replay key")
        suffix = f", {assignment}" if assignment else ""
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            try:
                cursor = database.execute(
                    f"UPDATE attempts SET state = ?{suffix} "
                    "WHERE replay_key = ? AND state = ?",
                    (state,) + tuple(values) + (replay_key, expected),
                )
                if cursor.rowcount != 1:
                    row = database.execute(
                        "SELECT state FROM attempts WHERE replay_key = ?", (replay_key,)
                    ).fetchone()
                    actual = "missing" if row is None else row["state"]
                    raise ReplayConflict(
                        f"execution cannot move from {actual} to {state}"
                    )
                database.execute("COMMIT")
            except Exception:
                database.execute("ROLLBACK")
                raise

    @staticmethod
    def _text(value, label):
        if not isinstance(value, str) or not value or "\x00" in value:
            raise StoreError(f"{label} must be non-empty text")
