"""Domain types exchanged between the vehicles, the hub and the operators."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from typing import Any

#: Operating modes an autonomous vehicle reports.
VEHICLE_MODES = ("autonomous", "manual", "teleop", "parked", "charging", "fault")

#: Commands the hub is allowed to dispatch. Anything else is rejected at the
#: API boundary so a compromised operator console cannot invent new verbs.
COMMAND_TYPES = (
    "pull_over",          # safe-stop at the next legal spot
    "return_to_depot",
    "set_geofence",
    "set_speed_limit",
    "software_update",    # payload names a package already staged in the depot
    "reboot_compute",
    "clear_fault",
    "ping",
)

#: Terminal + non-terminal command states.
COMMAND_PENDING = "pending"
COMMAND_LEASED = "leased"
COMMAND_ACKED = "acked"
COMMAND_FAILED = "failed"
COMMAND_EXPIRED = "expired"


class ValidationError(ValueError):
    """Raised when a payload from a vehicle or operator is malformed."""


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ValidationError(message)


def _number(value: Any, name: str, lo: float, hi: float) -> float:
    _require(isinstance(value, (int, float)) and not isinstance(value, bool), f"{name} must be a number")
    value = float(value)
    _require(math.isfinite(value), f"{name} must be finite")
    _require(lo <= value <= hi, f"{name} must be between {lo} and {hi}")
    return value


@dataclass
class Vehicle:
    """A provisioned member of the fleet."""

    vehicle_id: str
    name: str
    secret: str
    status: str = "active"          # active | quarantined | retired
    fw_version: str = "unknown"
    enrolled_at: float = field(default_factory=time.time)
    last_seen: float = 0.0

    def public(self) -> dict[str, Any]:
        """Representation safe to hand to an operator (no shared secret)."""
        return {
            "vehicle_id": self.vehicle_id,
            "name": self.name,
            "status": self.status,
            "fw_version": self.fw_version,
            "enrolled_at": self.enrolled_at,
            "last_seen": self.last_seen or None,
        }


@dataclass
class TelemetryPoint:
    """One sample from one vehicle.

    ``seq`` is the vehicle's own monotonic counter. It lets the hub detect gaps
    after a vehicle has been out of radio range, and it makes retried uploads
    idempotent: (vehicle_id, seq) is unique.
    """

    vehicle_id: str
    seq: int
    ts: float
    lat: float
    lon: float
    speed_mps: float
    heading_deg: float
    battery_soc: float
    mode: str
    faults: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    received_at: float = field(default_factory=time.time)

    @classmethod
    def from_payload(cls, vehicle_id: str, payload: Any) -> "TelemetryPoint":
        _require(isinstance(payload, dict), "telemetry point must be an object")
        seq = payload.get("seq")
        _require(isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0, "seq must be a non-negative integer")

        faults = payload.get("faults", [])
        _require(isinstance(faults, list), "faults must be a list")
        _require(all(isinstance(f, str) for f in faults), "faults must be a list of strings")
        _require(len(faults) <= 32, "at most 32 faults per point")

        mode = payload.get("mode", "autonomous")
        _require(mode in VEHICLE_MODES, f"mode must be one of {', '.join(VEHICLE_MODES)}")

        extra = payload.get("extra", {})
        _require(isinstance(extra, dict), "extra must be an object")

        return cls(
            vehicle_id=vehicle_id,
            seq=seq,
            ts=_number(payload.get("ts"), "ts", 0, 4_102_444_800),  # through year 2100
            lat=_number(payload.get("lat"), "lat", -90, 90),
            lon=_number(payload.get("lon"), "lon", -180, 180),
            speed_mps=_number(payload.get("speed_mps", 0.0), "speed_mps", -5, 90),
            heading_deg=_number(payload.get("heading_deg", 0.0), "heading_deg", 0, 360),
            battery_soc=_number(payload.get("battery_soc", 0.0), "battery_soc", 0, 100),
            mode=mode,
            faults=faults,
            extra=extra,
        )

    def as_row(self) -> tuple[Any, ...]:
        return (
            self.vehicle_id,
            self.seq,
            self.ts,
            self.received_at,
            self.lat,
            self.lon,
            self.speed_mps,
            self.heading_deg,
            self.battery_soc,
            self.mode,
            json.dumps(self.faults, separators=(",", ":")),
            json.dumps(self.extra, separators=(",", ":")),
        )

    def public(self) -> dict[str, Any]:
        return {
            "vehicle_id": self.vehicle_id,
            "seq": self.seq,
            "ts": self.ts,
            "received_at": self.received_at,
            "lat": self.lat,
            "lon": self.lon,
            "speed_mps": self.speed_mps,
            "heading_deg": self.heading_deg,
            "battery_soc": self.battery_soc,
            "mode": self.mode,
            "faults": self.faults,
            "extra": self.extra,
        }


@dataclass
class Command:
    """A unit of work the hub hands to a vehicle."""

    command_id: str
    vehicle_id: str
    type: str
    payload: dict[str, Any]
    state: str = COMMAND_PENDING
    attempts: int = 0
    created_at: float = field(default_factory=time.time)
    lease_expires_at: float = 0.0
    completed_at: float = 0.0
    result: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def validate(type_: Any, payload: Any) -> dict[str, Any]:
        _require(type_ in COMMAND_TYPES, f"type must be one of {', '.join(COMMAND_TYPES)}")
        if payload is None:
            payload = {}
        _require(isinstance(payload, dict), "payload must be an object")

        # Per-command payload rules. Kept here so both the HTTP API and any
        # future dispatcher share one definition of a well-formed command.
        if type_ == "set_speed_limit":
            _number(payload.get("limit_mps"), "payload.limit_mps", 0, 40)
        elif type_ == "set_geofence":
            points = payload.get("polygon")
            _require(isinstance(points, list) and len(points) >= 3, "payload.polygon needs at least 3 points")
            for point in points:
                _require(isinstance(point, list) and len(point) == 2, "polygon points must be [lat, lon]")
                _number(point[0], "polygon lat", -90, 90)
                _number(point[1], "polygon lon", -180, 180)
        elif type_ == "software_update":
            pkg = payload.get("package")
            _require(isinstance(pkg, str) and pkg.strip() != "", "payload.package is required")
            digest = payload.get("sha256")
            _require(isinstance(digest, str) and len(digest) == 64, "payload.sha256 must be a 64-char hex digest")
        return payload

    def public(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "vehicle_id": self.vehicle_id,
            "type": self.type,
            "payload": self.payload,
            "state": self.state,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "lease_expires_at": self.lease_expires_at or None,
            "completed_at": self.completed_at or None,
            "result": self.result,
        }
