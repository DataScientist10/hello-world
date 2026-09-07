"""Durable storage.

SQLite is the whole database tier: it needs no server process, no network and
no operator, which is exactly right for a box sitting in a depot with no
internet connection.

Two design points carry most of the weight:

* **One writer thread.** SQLite allows a single writer at a time, so instead of
  letting 450 vehicles fight over the write lock we funnel every mutation
  through one thread and let readers run concurrently in WAL mode.
* **Group commit.** The writer drains everything queued behind it and commits
  the batch in one transaction, so a burst of telemetry uploads costs one fsync
  rather than one per request. Callers still block until their own batch is
  committed, so an HTTP 200 always means "on disk" and a vehicle can safely
  drop the rows from its local spool.
"""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    secret      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    fw_version  TEXT NOT NULL DEFAULT 'unknown',
    enrolled_at REAL NOT NULL,
    last_seen   REAL NOT NULL DEFAULT 0
);

-- (vehicle_id, seq) is the idempotency key: a vehicle that retries an upload
-- after a dropped connection re-sends the same sequence numbers and the
-- duplicates are ignored rather than double-counted.
CREATE TABLE IF NOT EXISTS telemetry (
    vehicle_id   TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    ts           REAL NOT NULL,
    received_at  REAL NOT NULL,
    lat          REAL NOT NULL,
    lon          REAL NOT NULL,
    speed_mps    REAL NOT NULL,
    heading_deg  REAL NOT NULL,
    battery_soc  REAL NOT NULL,
    mode         TEXT NOT NULL,
    faults       TEXT NOT NULL DEFAULT '[]',
    extra        TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (vehicle_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_telemetry_received ON telemetry (received_at);
CREATE INDEX IF NOT EXISTS idx_telemetry_vehicle_ts ON telemetry (vehicle_id, ts DESC);

CREATE TABLE IF NOT EXISTS commands (
    command_id       TEXT PRIMARY KEY,
    vehicle_id       TEXT NOT NULL,
    type             TEXT NOT NULL,
    payload          TEXT NOT NULL DEFAULT '{}',
    state            TEXT NOT NULL DEFAULT 'pending',
    attempts         INTEGER NOT NULL DEFAULT 0,
    created_at       REAL NOT NULL,
    lease_expires_at REAL NOT NULL DEFAULT 0,
    completed_at     REAL NOT NULL DEFAULT 0,
    result           TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_commands_dispatch ON commands (vehicle_id, state, created_at);

-- Append-only operational history. With no remote logging to ship to, this is
-- the record an engineer reads the morning after an incident.
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    vehicle_id TEXT,
    kind       TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts DESC);
"""

TELEMETRY_INSERT = """
INSERT OR IGNORE INTO telemetry
    (vehicle_id, seq, ts, received_at, lat, lon, speed_mps, heading_deg,
     battery_soc, mode, faults, extra)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

#: How many queued jobs the writer will fold into one transaction.
MAX_GROUP_COMMIT = 256


class _Job:
    __slots__ = ("kind", "rows", "fn", "done", "result", "error")

    def __init__(self, kind: str, rows: Iterable[tuple] | None = None, fn: Callable | None = None) -> None:
        self.kind = kind
        self.rows = list(rows) if rows is not None else []
        self.fn = fn
        self.done = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None

    def wait(self, timeout: float | None = None) -> Any:
        if not self.done.wait(timeout):
            raise TimeoutError("timed out waiting for the database writer")
        if self.error is not None:
            raise self.error
        return self.result


class Storage:
    """Owns the SQLite file plus the single writer thread."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._queue: queue.Queue[_Job | None] = queue.Queue()
        self._closed = False
        self._writer_conn = self._connect()
        self._init_schema()
        self._writer = threading.Thread(target=self._writer_loop, name="hub-db-writer", daemon=True)
        self._writer.start()

    # -- connections --------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=15.0,
            isolation_level=None,       # explicit transactions only
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")     # readers never block the writer
        conn.execute("PRAGMA synchronous=NORMAL")   # durable across process crash; survives power loss with WAL
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _init_schema(self) -> None:
        self._writer_conn.executescript(SCHEMA)

    def read(self) -> sqlite3.Connection:
        """A read connection private to the calling thread."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    # -- writer thread ------------------------------------------------------
    def _writer_loop(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            batch = [job]
            while len(batch) < MAX_GROUP_COMMIT:
                try:
                    nxt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    self._queue.put(None)   # let the next iteration see the sentinel
                    break
                batch.append(nxt)

            point_jobs = [j for j in batch if j.kind == "points"]
            call_jobs = [j for j in batch if j.kind == "call"]

            if point_jobs:
                self._run_point_jobs(point_jobs)
            for call_job in call_jobs:
                self._run_call_job(call_job)

    def _run_point_jobs(self, jobs: list[_Job]) -> None:
        """Commit every queued telemetry batch in one transaction (one fsync).

        Each job gets its own ``executemany`` so ``total_changes`` gives an
        exact per-caller count of rows that were new rather than duplicates of
        an earlier retry.
        """
        try:
            self._writer_conn.execute("BEGIN IMMEDIATE")
            counts = []
            for job in jobs:
                before = self._writer_conn.total_changes
                self._writer_conn.executemany(TELEMETRY_INSERT, job.rows)
                counts.append(self._writer_conn.total_changes - before)
            self._writer_conn.execute("COMMIT")
        except BaseException as exc:            # noqa: BLE001 - reported to every caller
            self._safe_rollback()
            for job in jobs:
                job.error = exc
                job.done.set()
            return
        for job, inserted in zip(jobs, counts):
            job.result = inserted
            job.done.set()

    def _run_call_job(self, job: _Job) -> None:
        try:
            self._writer_conn.execute("BEGIN IMMEDIATE")
            job.result = job.fn(self._writer_conn)  # type: ignore[misc]
            self._writer_conn.execute("COMMIT")
        except BaseException as exc:            # noqa: BLE001 - reported to the caller
            self._safe_rollback()
            job.error = exc
        finally:
            job.done.set()

    def _safe_rollback(self) -> None:
        try:
            self._writer_conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    # -- public write API ---------------------------------------------------
    def write_points(self, rows: Iterable[tuple], timeout: float = 15.0) -> int:
        """Insert telemetry rows, returning how many were new. Blocks until committed."""
        rows = list(rows)
        if not rows:
            return 0
        job = _Job("points", rows=rows)
        self._submit(job)
        return job.wait(timeout)

    def execute(self, fn: Callable[[sqlite3.Connection], Any], timeout: float = 15.0) -> Any:
        """Run ``fn`` inside a write transaction on the writer thread."""
        job = _Job("call", fn=fn)
        self._submit(job)
        return job.wait(timeout)

    def _submit(self, job: _Job) -> None:
        if self._closed:
            raise RuntimeError("storage is closed")
        self._queue.put(job)

    # -- lifecycle ----------------------------------------------------------
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put(None)
        self._writer.join(timeout=15.0)
        try:
            self._writer_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        self._writer_conn.close()

    # -- helpers ------------------------------------------------------------
    def purge_telemetry(self, older_than: float) -> int:
        """Drop telemetry received before ``older_than``; returns rows removed."""

        def _purge(conn: sqlite3.Connection) -> int:
            cursor = conn.execute("DELETE FROM telemetry WHERE received_at < ?", (older_than,))
            return cursor.rowcount or 0

        return self.execute(_purge)

    def purge_oldest_telemetry(self, limit: int, older_than: float) -> int:
        """Delete up to ``limit`` of the oldest telemetry rows predating ``older_than``.

        The last resort when time-based retention is not freeing space fast
        enough. Two bounds matter: ``limit`` keeps one DELETE from stalling the
        writer thread -- and with it every vehicle waiting on a commit -- and
        ``older_than`` preserves a minimum window of recent history, so a full
        disk cannot cost an incident investigation the very samples it needs.
        """

        def _purge(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                "DELETE FROM telemetry WHERE rowid IN ("
                "  SELECT rowid FROM telemetry WHERE received_at < ?"
                "  ORDER BY received_at ASC LIMIT ?"
                ")",
                (older_than, limit),
            )
            return cursor.rowcount or 0

        return self.execute(_purge)

    def checkpoint_wal(self) -> None:
        """Fold the write-ahead log back into the database and truncate it.

        Under sustained write load the -wal file grows and holds disk that
        deleting rows alone will not give back, so this runs before any
        emergency purge is judged to have failed.
        """

        def _checkpoint(conn: sqlite3.Connection) -> None:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        try:
            self.execute(_checkpoint)
        except sqlite3.Error:
            pass                                # best effort; never fatal

    def telemetry_row_count(self) -> int:
        row = self.read().execute("SELECT COUNT(*) AS n FROM telemetry").fetchone()
        return row["n"] if row else 0

    def log_event(self, kind: str, vehicle_id: str | None = None, detail: dict | None = None) -> None:
        payload = json.dumps(detail or {}, separators=(",", ":"))
        now = time.time()

        def _insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO events (ts, vehicle_id, kind, detail) VALUES (?, ?, ?, ?)",
                (now, vehicle_id, kind, payload),
            )

        self.execute(_insert)

    def recent_events(self, limit: int = 100) -> list[dict]:
        rows = self.read().execute(
            "SELECT ts, vehicle_id, kind, detail FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            {"ts": r["ts"], "vehicle_id": r["vehicle_id"], "kind": r["kind"], "detail": json.loads(r["detail"])}
            for r in rows
        ]

    def database_size_bytes(self) -> int:
        try:
            return sum(
                p.stat().st_size
                for p in [self.db_path, Path(f"{self.db_path}-wal"), Path(f"{self.db_path}-shm")]
                if p.exists()
            )
        except OSError:
            return 0
