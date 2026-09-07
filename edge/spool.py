"""Durable store-and-forward buffer for telemetry.

A vehicle spends real time outside depot Wi-Fi coverage. Anything it measures
out there has to survive both the outage and a hard power cut, so samples land
in a small SQLite file on the vehicle before any upload is attempted, and are
deleted only once the hub has confirmed the write.

The spool is bounded. When it fills, the *oldest* samples are dropped: after a
long outage, the recent history of where the vehicle is now matters more than
where it was three hours ago, and the resulting sequence-number gap is
explicitly reported to the hub rather than silently hidden.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS spool (
    seq     INTEGER PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Spool:
    def __init__(self, path: Path | str, max_points: int = 20_000) -> None:
        self.path = Path(path)
        self.max_points = max_points
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")   # a vehicle can lose power without warning
        self._conn.executescript(SCHEMA)
        self._dropped = 0

    # -- sequence numbers ---------------------------------------------------
    def next_seq(self) -> int:
        """Allocate the next monotonic sequence number, persisted across reboots."""
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = 'next_seq'").fetchone()
            seq = int(row["value"]) if row else 1
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES ('next_seq', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(seq + 1),),
            )
            return seq

    # -- queue --------------------------------------------------------------
    def append(self, point: dict[str, Any]) -> None:
        """Buffer one sample. Assigns ``seq`` if the caller has not."""
        if "seq" not in point:
            point = {**point, "seq": self.next_seq()}
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "INSERT OR REPLACE INTO spool (seq, payload) VALUES (?, ?)",
                (point["seq"], json.dumps(point, separators=(",", ":"))),
            )
            overflow = self._conn.execute("SELECT COUNT(*) AS n FROM spool").fetchone()["n"] - self.max_points
            if overflow > 0:
                self._conn.execute(
                    "DELETE FROM spool WHERE seq IN (SELECT seq FROM spool ORDER BY seq ASC LIMIT ?)",
                    (overflow,),
                )
                self._dropped += overflow
            self._conn.execute("COMMIT")

    def peek(self, limit: int) -> list[dict[str, Any]]:
        """The oldest ``limit`` samples, still buffered until acknowledged."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, payload FROM spool ORDER BY seq ASC LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def release(self, seqs: list[int]) -> int:
        """Drop samples the hub has confirmed it stored."""
        if not seqs:
            return 0
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.executemany("DELETE FROM spool WHERE seq = ?", [(s,) for s in seqs])
            self._conn.execute("COMMIT")
        return len(seqs)

    def depth(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) AS n FROM spool").fetchone()["n"]

    @property
    def dropped(self) -> int:
        """Samples discarded to overflow since this process started."""
        return self._dropped

    def close(self) -> None:
        with self._lock:
            self._conn.close()
