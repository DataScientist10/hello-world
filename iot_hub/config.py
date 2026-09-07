"""Runtime configuration.

Every value has a default that works on a fresh depot server, and every value
can be overridden with an environment variable so the service can be tuned
without editing code (see deploy/iot-hub.env).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

#: The fleet this hub was sized for. Used by the provisioning CLI and by the
#: capacity warnings in the readiness probe.
FLEET_SIZE = 450


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    """Hub configuration."""

    # --- network -----------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8080
    #: Optional TLS material. Both must be set to enable HTTPS. The certificate
    #: is expected to come from the depot's own offline CA (see
    #: docs/OFFLINE_DEPLOYMENT.md); no public CA is ever contacted.
    tls_cert: str | None = None
    tls_key: str | None = None

    # --- storage -----------------------------------------------------------
    data_dir: Path = Path("/var/lib/iot-hub")
    #: Telemetry older than this is purged so the disk footprint stays bounded
    #: on a server nobody can reach remotely.
    telemetry_retention_hours: int = 72
    purge_interval_seconds: int = 900

    # --- security ----------------------------------------------------------
    #: Requests whose timestamp is further away than this are rejected. Keeps
    #: the replay-nonce cache small and bounds clock drift on vehicles that
    #: have no NTP upstream.
    clock_skew_tolerance_seconds: int = 300
    #: Shared secret an unprovisioned vehicle presents to /v1/enroll. Empty
    #: disables online enrollment entirely (offline provisioning only).
    provisioning_key: str = ""
    #: Key operators/back-office tools present on the read + command API.
    operator_key: str = ""

    # --- ingest ------------------------------------------------------------
    max_body_bytes: int = 1_048_576
    max_batch_points: int = 500

    # --- command dispatch --------------------------------------------------
    #: Upper bound on how long a vehicle may hold a long-poll open. Must stay
    #: below any idle timeout on intermediate depot switches/proxies.
    max_long_poll_seconds: float = 25.0
    #: How long a leased command stays invisible before it is redelivered.
    command_lease_seconds: int = 60
    command_max_attempts: int = 5

    # --- fleet -------------------------------------------------------------
    fleet_size: int = FLEET_SIZE
    #: A vehicle is "stale" once it has not reported for this long.
    vehicle_offline_after_seconds: int = 90

    #: Populated in __post_init__; not read from the environment.
    db_path: Path = field(init=False)

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        self.db_path = self.data_dir / "hub.db"

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls(
            host=os.environ.get("HUB_HOST", "0.0.0.0"),
            port=_env_int("HUB_PORT", 8080),
            tls_cert=os.environ.get("HUB_TLS_CERT") or None,
            tls_key=os.environ.get("HUB_TLS_KEY") or None,
            data_dir=Path(os.environ.get("HUB_DATA_DIR", "/var/lib/iot-hub")),
            telemetry_retention_hours=_env_int("HUB_TELEMETRY_RETENTION_HOURS", 72),
            purge_interval_seconds=_env_int("HUB_PURGE_INTERVAL_SECONDS", 900),
            clock_skew_tolerance_seconds=_env_int("HUB_CLOCK_SKEW_SECONDS", 300),
            provisioning_key=os.environ.get("HUB_PROVISIONING_KEY", ""),
            operator_key=os.environ.get("HUB_OPERATOR_KEY", ""),
            max_body_bytes=_env_int("HUB_MAX_BODY_BYTES", 1_048_576),
            max_batch_points=_env_int("HUB_MAX_BATCH_POINTS", 500),
            max_long_poll_seconds=_env_float("HUB_MAX_LONG_POLL_SECONDS", 25.0),
            command_lease_seconds=_env_int("HUB_COMMAND_LEASE_SECONDS", 60),
            command_max_attempts=_env_int("HUB_COMMAND_MAX_ATTEMPTS", 5),
            fleet_size=_env_int("HUB_FLEET_SIZE", FLEET_SIZE),
            vehicle_offline_after_seconds=_env_int("HUB_VEHICLE_OFFLINE_AFTER", 90),
        )
        if _env_bool("HUB_REQUIRE_TLS", False) and not cfg.tls_enabled:
            raise ValueError("HUB_REQUIRE_TLS is set but HUB_TLS_CERT/HUB_TLS_KEY are not")
        return cfg

    @property
    def tls_enabled(self) -> bool:
        return bool(self.tls_cert and self.tls_key)
