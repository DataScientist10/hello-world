"""Hub-to-vehicle command dispatch over plain HTTP.

There is no MQTT broker and no WebSocket gateway here, because on an isolated
depot network the fewest moving parts wins. Downlink instead works the way
every HTTP-only system does it: the vehicle **long-polls**. It asks for work
and the hub holds the connection open for up to ~25 seconds; if a command is
queued in that window the vehicle is answered immediately, otherwise it gets an
empty response and asks again. Latency is milliseconds, and the vehicle never
needs an inbound port or a fixed address.

Delivery is at-least-once with a visibility lease: a command handed out is
hidden for ``command_lease_seconds`` and redelivered if the vehicle never
acknowledges it (drove out of Wi-Fi range mid-command, rebooted, crashed).
Vehicles must therefore treat ``command_id`` as an idempotency key.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid

from .models import (
    COMMAND_ACKED,
    COMMAND_FAILED,
    COMMAND_LEASED,
    COMMAND_PENDING,
    Command,
)

_SELECT_COLUMNS = (
    "command_id, vehicle_id, type, payload, state, attempts, created_at, "
    "lease_expires_at, completed_at, result"
)


def _row_to_command(row: sqlite3.Row) -> Command:
    return Command(
        command_id=row["command_id"],
        vehicle_id=row["vehicle_id"],
        type=row["type"],
        payload=json.loads(row["payload"]),
        state=row["state"],
        attempts=row["attempts"],
        created_at=row["created_at"],
        lease_expires_at=row["lease_expires_at"],
        completed_at=row["completed_at"],
        result=json.loads(row["result"]),
    )


class CommandQueue:
    def __init__(self, storage, config) -> None:
        self._storage = storage
        self._config = config
        self._lock = threading.Lock()
        #: One event per vehicle so enqueueing work for one vehicle does not
        #: wake the other 449 threads parked in long-poll.
        self._waiters: dict[str, threading.Event] = {}

    def _event_for(self, vehicle_id: str) -> threading.Event:
        with self._lock:
            event = self._waiters.get(vehicle_id)
            if event is None:
                event = threading.Event()
                self._waiters[vehicle_id] = event
            return event

    def _wake(self, vehicle_id: str) -> None:
        with self._lock:
            event = self._waiters.get(vehicle_id)
        if event is not None:
            event.set()

    # -- producer -----------------------------------------------------------
    def enqueue(self, vehicle_id: str, type_: str, payload: dict | None = None, issued_by: str = "operator") -> Command:
        payload = Command.validate(type_, payload)
        command = Command(
            command_id=str(uuid.uuid4()),
            vehicle_id=vehicle_id,
            type=type_,
            payload=payload,
        )
        encoded = json.dumps(payload, separators=(",", ":"))

        def _insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO commands (command_id, vehicle_id, type, payload, state, attempts, created_at)"
                " VALUES (?, ?, ?, ?, 'pending', 0, ?)",
                (command.command_id, vehicle_id, type_, encoded, command.created_at),
            )

        self._storage.execute(_insert)
        self._storage.log_event(
            "command.enqueued", vehicle_id,
            {"command_id": command.command_id, "type": type_, "issued_by": issued_by},
        )
        self._wake(vehicle_id)
        return command

    def enqueue_bulk(self, vehicle_ids: list[str], type_: str, payload: dict | None = None,
                     issued_by: str = "operator") -> list[Command]:
        """Queue the same command for many vehicles in one transaction.

        This is the fleet-wide safety lever ("every vehicle pull over now"), so
        it must not degrade into 450 separate commits: one transaction, then
        one wake-up per vehicle.
        """
        payload = Command.validate(type_, payload)
        encoded = json.dumps(payload, separators=(",", ":"))
        now = time.time()
        commands = [
            Command(command_id=str(uuid.uuid4()), vehicle_id=vid, type=type_, payload=payload, created_at=now)
            for vid in vehicle_ids
        ]
        if not commands:
            return []

        rows = [(c.command_id, c.vehicle_id, type_, encoded, now) for c in commands]

        def _insert(conn: sqlite3.Connection) -> None:
            conn.executemany(
                "INSERT INTO commands (command_id, vehicle_id, type, payload, state, attempts, created_at)"
                " VALUES (?, ?, ?, ?, 'pending', 0, ?)",
                rows,
            )

        self._storage.execute(_insert)
        self._storage.log_event(
            "command.broadcast", None,
            {"type": type_, "vehicles": len(commands), "issued_by": issued_by},
        )
        for command in commands:
            self._wake(command.vehicle_id)
        return commands

    # -- consumer -----------------------------------------------------------
    def lease(self, vehicle_id: str, max_commands: int = 8, wait_seconds: float = 0.0) -> list[Command]:
        """Hand pending commands to a vehicle, waiting up to ``wait_seconds``."""
        wait_seconds = max(0.0, min(wait_seconds, self._config.max_long_poll_seconds))
        deadline = time.monotonic() + wait_seconds
        event = self._event_for(vehicle_id)

        while True:
            # Clear before reading so a command enqueued during the read is not
            # lost -- it re-sets the event and the next wait returns at once.
            event.clear()
            leased = self._lease_now(vehicle_id, max_commands)
            if leased:
                return leased
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            event.wait(remaining)

    def _lease_now(self, vehicle_id: str, max_commands: int) -> list[Command]:
        now = time.time()
        lease_until = now + self._config.command_lease_seconds
        max_attempts = self._config.command_max_attempts

        def _lease(conn: sqlite3.Connection) -> list[Command]:
            rows = conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM commands"
                " WHERE vehicle_id = ?"
                "   AND (state = 'pending' OR (state = 'leased' AND lease_expires_at <= ?))"
                " ORDER BY created_at ASC LIMIT ?",
                (vehicle_id, now, max_commands),
            ).fetchall()

            handed_out: list[Command] = []
            for row in rows:
                command = _row_to_command(row)
                if command.attempts >= max_attempts:
                    # Undeliverable: park it so an operator can see it rather
                    # than looping forever against a vehicle that never acks.
                    conn.execute(
                        "UPDATE commands SET state = ?, completed_at = ?, result = ? WHERE command_id = ?",
                        (COMMAND_FAILED, now,
                         json.dumps({"error": "max delivery attempts exceeded"}, separators=(",", ":")),
                         command.command_id),
                    )
                    continue
                command.attempts += 1
                command.state = COMMAND_LEASED
                command.lease_expires_at = lease_until
                conn.execute(
                    "UPDATE commands SET state = ?, attempts = ?, lease_expires_at = ? WHERE command_id = ?",
                    (COMMAND_LEASED, command.attempts, lease_until, command.command_id),
                )
                handed_out.append(command)
            return handed_out

        return self._storage.execute(_lease)

    def acknowledge(self, vehicle_id: str, command_id: str, ok: bool, result: dict | None = None) -> Command:
        """Close out a command the vehicle has finished (or failed)."""
        now = time.time()
        state = COMMAND_ACKED if ok else COMMAND_FAILED
        encoded = json.dumps(result or {}, separators=(",", ":"))

        def _ack(conn: sqlite3.Connection) -> Command:
            row = conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM commands WHERE command_id = ?", (command_id,)
            ).fetchone()
            if row is None:
                raise KeyError(command_id)
            if row["vehicle_id"] != vehicle_id:
                # Never let one vehicle close another vehicle's command.
                raise PermissionError(command_id)
            command = _row_to_command(row)
            if command.state in (COMMAND_ACKED, COMMAND_FAILED):
                return command        # idempotent: a retried ack is a no-op
            conn.execute(
                "UPDATE commands SET state = ?, completed_at = ?, result = ?, lease_expires_at = 0"
                " WHERE command_id = ?",
                (state, now, encoded, command_id),
            )
            command.state = state
            command.completed_at = now
            command.result = result or {}
            return command

        command = self._storage.execute(_ack)
        self._storage.log_event(
            "command.acknowledged", vehicle_id,
            {"command_id": command_id, "state": command.state, "result": result or {}},
        )
        return command

    # -- maintenance --------------------------------------------------------
    def sweep_expired_leases(self) -> int:
        """Return leases past their expiry to the pending pool.

        Called on a timer. Vehicles waiting in long-poll are woken so a
        redelivery does not have to wait for their next request.
        """
        now = time.time()

        def _sweep(conn: sqlite3.Connection) -> list[str]:
            rows = conn.execute(
                "SELECT command_id, vehicle_id FROM commands WHERE state = 'leased' AND lease_expires_at <= ?",
                (now,),
            ).fetchall()
            if rows:
                conn.execute(
                    "UPDATE commands SET state = 'pending', lease_expires_at = 0"
                    " WHERE state = 'leased' AND lease_expires_at <= ?",
                    (now,),
                )
            return [row["vehicle_id"] for row in rows]

        vehicle_ids = self._storage.execute(_sweep)
        for vehicle_id in set(vehicle_ids):
            self._wake(vehicle_id)
        return len(vehicle_ids)

    # -- queries ------------------------------------------------------------
    def get(self, command_id: str) -> Command | None:
        row = self._storage.read().execute(
            f"SELECT {_SELECT_COLUMNS} FROM commands WHERE command_id = ?", (command_id,)
        ).fetchone()
        return _row_to_command(row) if row else None

    def list_for_vehicle(self, vehicle_id: str, limit: int = 50, state: str | None = None) -> list[Command]:
        sql = f"SELECT {_SELECT_COLUMNS} FROM commands WHERE vehicle_id = ?"
        params: list = [vehicle_id]
        if state:
            sql += " AND state = ?"
            params.append(state)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [_row_to_command(r) for r in self._storage.read().execute(sql, params).fetchall()]

    def counts_by_state(self) -> dict[str, int]:
        rows = self._storage.read().execute(
            "SELECT state, COUNT(*) AS n FROM commands GROUP BY state"
        ).fetchall()
        counts = {state: 0 for state in (COMMAND_PENDING, COMMAND_LEASED, COMMAND_ACKED, COMMAND_FAILED)}
        counts.update({row["state"]: row["n"] for row in rows})
        return counts

    @property
    def waiting_vehicles(self) -> int:
        with self._lock:
            return sum(1 for event in self._waiters.values() if not event.is_set())
