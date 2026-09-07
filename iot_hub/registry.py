"""The fleet roster.

Every authenticated request needs the vehicle's shared secret, so the whole
roster is held in memory and written through to SQLite. A 450-vehicle fleet is
a few hundred kilobytes, which turns per-request authentication into a dict
lookup instead of a database round trip.
"""

from __future__ import annotations

import sqlite3
import threading
import time

from .auth import generate_secret
from .models import Vehicle

VALID_STATUSES = ("active", "quarantined", "retired")


class VehicleExists(Exception):
    """Raised when enrolling a vehicle_id that is already on the roster."""


class Registry:
    def __init__(self, storage) -> None:
        self._storage = storage
        self._lock = threading.RLock()
        self._vehicles: dict[str, Vehicle] = {}
        self._load()

    def _load(self) -> None:
        rows = self._storage.read().execute(
            "SELECT vehicle_id, name, secret, status, fw_version, enrolled_at, last_seen FROM vehicles"
        ).fetchall()
        with self._lock:
            self._vehicles = {
                row["vehicle_id"]: Vehicle(
                    vehicle_id=row["vehicle_id"],
                    name=row["name"],
                    secret=row["secret"],
                    status=row["status"],
                    fw_version=row["fw_version"],
                    enrolled_at=row["enrolled_at"],
                    last_seen=row["last_seen"],
                )
                for row in rows
            }

    # -- lookups ------------------------------------------------------------
    def get(self, vehicle_id: str) -> Vehicle | None:
        with self._lock:
            return self._vehicles.get(vehicle_id)

    def all(self) -> list[Vehicle]:
        with self._lock:
            return sorted(self._vehicles.values(), key=lambda v: v.vehicle_id)

    def count(self) -> int:
        with self._lock:
            return len(self._vehicles)

    # -- mutations ----------------------------------------------------------
    def enroll(self, vehicle_id: str, name: str = "", fw_version: str = "unknown", secret: str | None = None) -> Vehicle:
        """Add a vehicle to the roster and return it *including* its secret.

        The secret is returned exactly once, at enrollment. It is the caller's
        job to burn it into the vehicle; the hub will never disclose it again.
        """
        vehicle_id = vehicle_id.strip()
        if not vehicle_id:
            raise ValueError("vehicle_id is required")
        if len(vehicle_id) > 64:
            raise ValueError("vehicle_id must be at most 64 characters")

        vehicle = Vehicle(
            vehicle_id=vehicle_id,
            name=name or vehicle_id,
            secret=secret or generate_secret(),
            fw_version=fw_version or "unknown",
            enrolled_at=time.time(),
        )

        def _insert(conn: sqlite3.Connection) -> None:
            try:
                conn.execute(
                    "INSERT INTO vehicles (vehicle_id, name, secret, status, fw_version, enrolled_at, last_seen)"
                    " VALUES (?, ?, ?, ?, ?, ?, 0)",
                    (vehicle.vehicle_id, vehicle.name, vehicle.secret, vehicle.status,
                     vehicle.fw_version, vehicle.enrolled_at),
                )
            except sqlite3.IntegrityError as exc:
                raise VehicleExists(vehicle.vehicle_id) from exc

        self._storage.execute(_insert)
        with self._lock:
            self._vehicles[vehicle.vehicle_id] = vehicle
        self._storage.log_event("vehicle.enrolled", vehicle.vehicle_id, {"name": vehicle.name})
        return vehicle

    def set_status(self, vehicle_id: str, status: str) -> Vehicle:
        """Quarantine, retire or re-activate a vehicle.

        Quarantining is the lever an operator pulls when a vehicle starts
        misbehaving: it keeps the roster entry and the history but the hub stops
        accepting the vehicle's signed requests immediately.
        """
        if status not in VALID_STATUSES:
            raise ValueError(f"status must be one of {', '.join(VALID_STATUSES)}")
        with self._lock:
            vehicle = self._vehicles.get(vehicle_id)
            if vehicle is None:
                raise KeyError(vehicle_id)

        def _update(conn: sqlite3.Connection) -> None:
            conn.execute("UPDATE vehicles SET status = ? WHERE vehicle_id = ?", (status, vehicle_id))

        self._storage.execute(_update)
        with self._lock:
            vehicle.status = status
        self._storage.log_event("vehicle.status_changed", vehicle_id, {"status": status})
        return vehicle

    def rotate_secret(self, vehicle_id: str) -> Vehicle:
        """Issue a new shared secret (returned once) for a vehicle."""
        with self._lock:
            vehicle = self._vehicles.get(vehicle_id)
            if vehicle is None:
                raise KeyError(vehicle_id)
        new_secret = generate_secret()

        def _update(conn: sqlite3.Connection) -> None:
            conn.execute("UPDATE vehicles SET secret = ? WHERE vehicle_id = ?", (new_secret, vehicle_id))

        self._storage.execute(_update)
        with self._lock:
            vehicle.secret = new_secret
        self._storage.log_event("vehicle.secret_rotated", vehicle_id)
        return vehicle

    def touch(self, vehicle_id: str, when: float | None = None, fw_version: str | None = None) -> None:
        """Record contact from a vehicle.

        Deliberately in-memory only on the hot path: ``last_seen`` is a
        liveness signal, not an accounting record, and writing it to disk on
        every request would triple the fleet's write volume. It is flushed by
        :meth:`flush_last_seen` on a timer and at shutdown.
        """
        when = time.time() if when is None else when
        with self._lock:
            vehicle = self._vehicles.get(vehicle_id)
            if vehicle is None:
                return
            vehicle.last_seen = when
            if fw_version and fw_version != vehicle.fw_version:
                vehicle.fw_version = fw_version
                changed_fw = fw_version
            else:
                changed_fw = None
        if changed_fw:
            def _update(conn: sqlite3.Connection) -> None:
                conn.execute("UPDATE vehicles SET fw_version = ? WHERE vehicle_id = ?", (changed_fw, vehicle_id))

            self._storage.execute(_update)
            self._storage.log_event("vehicle.firmware_reported", vehicle_id, {"fw_version": changed_fw})

    def flush_last_seen(self) -> int:
        """Persist in-memory ``last_seen`` values; returns rows updated."""
        with self._lock:
            pairs = [(v.last_seen, v.vehicle_id) for v in self._vehicles.values() if v.last_seen]
        if not pairs:
            return 0

        def _update(conn: sqlite3.Connection) -> int:
            conn.executemany("UPDATE vehicles SET last_seen = ? WHERE vehicle_id = ?", pairs)
            return len(pairs)

        return self._storage.execute(_update)

    def online_counts(self, offline_after: float, now: float | None = None) -> dict[str, int]:
        """Split the roster into online / offline / non-active buckets."""
        now = time.time() if now is None else now
        online = offline = inactive = 0
        with self._lock:
            for vehicle in self._vehicles.values():
                if vehicle.status != "active":
                    inactive += 1
                elif vehicle.last_seen and now - vehicle.last_seen <= offline_after:
                    online += 1
                else:
                    offline += 1
        return {"online": online, "offline": offline, "inactive": inactive}
