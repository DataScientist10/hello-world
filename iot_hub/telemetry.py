"""Telemetry ingest and the live fleet picture.

Vehicles buffer samples locally and upload them in batches, so the hot path
here is "validate a batch, write it once, update the in-memory view". The live
view is kept in memory rather than queried from SQLite because an operator
console refreshing a 450-vehicle map every second should not turn into 450
index scans.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from .models import TelemetryPoint, ValidationError


class IngestResult:
    __slots__ = ("accepted", "duplicates", "rejected", "errors", "gap")

    def __init__(self) -> None:
        self.accepted = 0
        self.duplicates = 0
        self.rejected = 0
        self.errors: list[str] = []
        #: Set when the vehicle's sequence numbers jumped, i.e. samples were
        #: lost for good (spool overflow) rather than merely delayed.
        self.gap: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "errors": self.errors[:10],
            "gap": self.gap,
        }


class TelemetryService:
    def __init__(self, storage, config, registry) -> None:
        self._storage = storage
        self._config = config
        self._registry = registry
        self._lock = threading.Lock()
        #: vehicle_id -> most recent point seen (the live fleet map)
        self._latest: dict[str, TelemetryPoint] = {}
        #: vehicle_id -> highest sequence number accepted, for gap detection
        self._high_water: dict[str, int] = {}
        self._load_latest()

    def _load_latest(self) -> None:
        """Warm the in-memory view from disk so a hub restart is invisible to operators."""
        rows = self._storage.read().execute(
            "SELECT t.* FROM telemetry t"
            " JOIN (SELECT vehicle_id, MAX(seq) AS seq FROM telemetry GROUP BY vehicle_id) m"
            "   ON t.vehicle_id = m.vehicle_id AND t.seq = m.seq"
        ).fetchall()
        with self._lock:
            for row in rows:
                point = TelemetryPoint(
                    vehicle_id=row["vehicle_id"],
                    seq=row["seq"],
                    ts=row["ts"],
                    lat=row["lat"],
                    lon=row["lon"],
                    speed_mps=row["speed_mps"],
                    heading_deg=row["heading_deg"],
                    battery_soc=row["battery_soc"],
                    mode=row["mode"],
                    faults=json.loads(row["faults"]),
                    extra=json.loads(row["extra"]),
                    received_at=row["received_at"],
                )
                self._latest[point.vehicle_id] = point
                self._high_water[point.vehicle_id] = point.seq

    # -- ingest -------------------------------------------------------------
    def ingest(self, vehicle_id: str, points_payload: Any) -> IngestResult:
        result = IngestResult()
        if not isinstance(points_payload, list):
            raise ValidationError("points must be a list")
        if len(points_payload) > self._config.max_batch_points:
            raise ValidationError(f"at most {self._config.max_batch_points} points per batch")

        parsed: list[TelemetryPoint] = []
        for index, raw in enumerate(points_payload):
            try:
                parsed.append(TelemetryPoint.from_payload(vehicle_id, raw))
            except ValidationError as exc:
                # One bad sample must not cost the vehicle the whole batch --
                # it would just retry the same batch forever.
                result.rejected += 1
                result.errors.append(f"point[{index}]: {exc}")

        if not parsed:
            return result

        inserted = self._storage.write_points([p.as_row() for p in parsed])
        result.accepted = inserted
        result.duplicates = len(parsed) - inserted

        newest = max(parsed, key=lambda p: p.seq)
        lowest_seq = min(p.seq for p in parsed)
        with self._lock:
            previous_high = self._high_water.get(vehicle_id)
            if previous_high is not None and lowest_seq > previous_high + 1:
                result.gap = lowest_seq - previous_high - 1
            if previous_high is None or newest.seq > previous_high:
                self._high_water[vehicle_id] = newest.seq
            current = self._latest.get(vehicle_id)
            if current is None or newest.seq >= current.seq:
                self._latest[vehicle_id] = newest

        if result.gap:
            self._storage.log_event(
                "telemetry.gap", vehicle_id,
                {"missing": result.gap, "resumed_at_seq": lowest_seq},
            )
        if newest.faults:
            self._storage.log_event("vehicle.faults", vehicle_id, {"faults": newest.faults, "mode": newest.mode})
        return result

    # -- live view ----------------------------------------------------------
    def latest(self, vehicle_id: str) -> TelemetryPoint | None:
        with self._lock:
            return self._latest.get(vehicle_id)

    def snapshot(self) -> list[TelemetryPoint]:
        with self._lock:
            return list(self._latest.values())

    def history(self, vehicle_id: str, limit: int = 100, since: float | None = None) -> list[dict]:
        sql = "SELECT * FROM telemetry WHERE vehicle_id = ?"
        params: list = [vehicle_id]
        if since is not None:
            sql += " AND ts >= ?"
            params.append(since)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        rows = self._storage.read().execute(sql, params).fetchall()
        return [
            {
                "vehicle_id": r["vehicle_id"], "seq": r["seq"], "ts": r["ts"],
                "received_at": r["received_at"], "lat": r["lat"], "lon": r["lon"],
                "speed_mps": r["speed_mps"], "heading_deg": r["heading_deg"],
                "battery_soc": r["battery_soc"], "mode": r["mode"],
                "faults": json.loads(r["faults"]), "extra": json.loads(r["extra"]),
            }
            for r in rows
        ]

    # -- aggregates ---------------------------------------------------------
    def fleet_summary(self, now: float | None = None) -> dict[str, Any]:
        """The one call an operator dashboard needs to render the fleet."""
        now = time.time() if now is None else now
        offline_after = self._config.vehicle_offline_after_seconds
        modes: dict[str, int] = {}
        faulted: list[str] = []
        low_battery: list[str] = []
        reporting = 0
        battery_total = 0.0

        with self._lock:
            points = list(self._latest.values())

        for point in points:
            fresh = now - point.received_at <= offline_after
            if fresh:
                reporting += 1
                modes[point.mode] = modes.get(point.mode, 0) + 1
                battery_total += point.battery_soc
                if point.faults:
                    faulted.append(point.vehicle_id)
                if point.battery_soc < 20:
                    low_battery.append(point.vehicle_id)

        counts = self._registry.online_counts(offline_after, now)
        return {
            "fleet_size": self._registry.count(),
            "expected_fleet_size": self._config.fleet_size,
            "online": counts["online"],
            "offline": counts["offline"],
            "inactive": counts["inactive"],
            "reporting": reporting,
            "modes": modes,
            "mean_battery_soc": round(battery_total / reporting, 2) if reporting else None,
            "faulted_vehicles": sorted(faulted),
            "low_battery_vehicles": sorted(low_battery),
            "generated_at": now,
        }
