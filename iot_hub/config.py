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

    # --- disk pressure -----------------------------------------------------
    #: Retention is a bet on the sample rate and the disk staying as configured.
    #: These are the backstop for when that bet is wrong. Below the warning
    #: line the hub purges harder than retention alone would; below the
    #: critical line it also stops accepting telemetry so the remaining space
    #: is kept for command dispatch. See iot_hub/diskguard.py.
    #: Thresholds are a ratio of the volume *capped by an absolute headroom*,
    #: because neither measure alone survives both ends of the range. On a
    #: 270 GB disk, 12% free is 32 GB -- roughly five days at the measured
    #: 5.8 GB/day -- and warning about it is noise; on a 20 GB disk, 12% is
    #: two days and genuinely urgent. So: warn below min(20% , 20 GiB) and
    #: shed below max(512 MiB, min(7%, 5 GiB)). At the fleet's measured burn
    #: rate that is about 3.5 days of headroom to the warning and under a day
    #: to shedding.
    disk_warn_free_ratio: float = 0.20
    disk_warn_free_cap_bytes: int = 20 * 1024 * 1024 * 1024
    disk_critical_free_ratio: float = 0.07
    disk_critical_free_cap_bytes: int = 5 * 1024 * 1024 * 1024
    #: Hard floor regardless of disk size: SQLite needs room to delete rows.
    disk_min_free_bytes: int = 512 * 1024 * 1024
    disk_check_interval_seconds: int = 60
    #: Never trim telemetry below this, however hard the disk is squeezing --
    #: an incident investigation needs *some* history to look at.
    disk_min_retention_hours: int = 1
    #: Rows deleted per emergency-purge batch, so the writer thread is never
    #: blocked for long by one enormous DELETE.
    disk_purge_batch_rows: int = 50_000

    # --- security ----------------------------------------------------------
    #: Requests whose timestamp is further away than this are rejected. Keeps
    #: the replay-nonce cache small and bounds clock drift on vehicles that
    #: have no NTP upstream.
    clock_skew_tolerance_seconds: int = 300
    #: Shared secret an unprovisioned vehicle presents to /v1/enroll. Empty
    #: disables online enrollment entirely (offline provisioning only).
    provisioning_key: str = ""
    #: Key operators/back-office tools present on the read + command API.
    #: Full scope: reads, commands, enrolment, secret rotation.
    operator_key: str = ""
    #: A second key with read-only scope, for anything that only needs to *see*
    #: the fleet -- a wall dashboard, a monitoring box, a shift supervisor's
    #: browser tab. Those consumers should not be holding a credential that can
    #: broadcast a command or mint a vehicle secret, which is what the full
    #: operator key can do. Presented on the same header; the hub decides the
    #: scope, so a client cannot widen its own access by choosing a header.
    viewer_key: str = ""

    #: Serve the operator console at /console. The page ships no credential and
    #: is inert until someone supplies a key, so this is a static asset rather
    #: than an access path -- but it is still surface, and a depot that drives
    #: its hub purely from the CLI can switch it off.
    console_enabled: bool = True
    #: Operator and provisioning keys are static bearer tokens: unlike a vehicle
    #: signature, the secret itself crosses the wire on every request, and the
    #: operator API can hand out vehicle secrets. Over plain HTTP on an
    #: untrusted LAN one passive tap captures it, so TLS is required unless an
    #: operator deliberately opts out (local testing, a trusted loopback).
    allow_insecure_operator_api: bool = False

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
            disk_warn_free_ratio=_env_float("HUB_DISK_WARN_FREE_RATIO", 0.20),
            disk_warn_free_cap_bytes=_env_int("HUB_DISK_WARN_FREE_CAP_BYTES", 20 * 1024 * 1024 * 1024),
            disk_critical_free_ratio=_env_float("HUB_DISK_CRITICAL_FREE_RATIO", 0.07),
            disk_critical_free_cap_bytes=_env_int("HUB_DISK_CRITICAL_FREE_CAP_BYTES", 5 * 1024 * 1024 * 1024),
            disk_min_free_bytes=_env_int("HUB_DISK_MIN_FREE_BYTES", 512 * 1024 * 1024),
            disk_check_interval_seconds=_env_int("HUB_DISK_CHECK_INTERVAL_SECONDS", 60),
            disk_min_retention_hours=_env_int("HUB_DISK_MIN_RETENTION_HOURS", 1),
            disk_purge_batch_rows=_env_int("HUB_DISK_PURGE_BATCH_ROWS", 50_000),
            clock_skew_tolerance_seconds=_env_int("HUB_CLOCK_SKEW_SECONDS", 300),
            provisioning_key=os.environ.get("HUB_PROVISIONING_KEY", ""),
            operator_key=os.environ.get("HUB_OPERATOR_KEY", ""),
            viewer_key=os.environ.get("HUB_VIEWER_KEY", ""),
            console_enabled=_env_bool("HUB_CONSOLE_ENABLED", True),
            allow_insecure_operator_api=_env_bool("HUB_ALLOW_INSECURE_OPERATOR_API", False),
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
        cfg.check_operator_channel()
        return cfg

    def check_operator_channel(self) -> None:
        """Fail fast rather than leak a privileged credential in cleartext.

        A vehicle proves possession of its secret without transmitting it. The
        operator key is the opposite -- it is sent verbatim on every request and
        never expires -- and it can mint vehicle secrets via rotate-secret, so
        capturing it defeats the vehicle scheme entirely.
        """
        if self.viewer_key and self.viewer_key == self.operator_key:
            raise ValueError(
                "HUB_VIEWER_KEY is identical to HUB_OPERATOR_KEY, so the read-only key "
                "would carry full operator scope. Generate a separate value."
            )
        if not (self.operator_key or self.provisioning_key or self.viewer_key):
            return
        if self.tls_enabled or self.allow_insecure_operator_api:
            return
        raise ValueError(
            "HUB_OPERATOR_KEY/HUB_PROVISIONING_KEY/HUB_VIEWER_KEY is set without TLS. These are bearer "
            "tokens sent in cleartext and can be captured by anyone on the LAN. "
            "Set HUB_TLS_CERT/HUB_TLS_KEY, or set HUB_ALLOW_INSECURE_OPERATOR_API=true "
            "to accept that risk deliberately (testing or a trusted loopback only)."
        )

    def disk_warn_bytes(self, total_bytes: int) -> float:
        """Free-space level below which the hub purges harder than retention alone."""
        return min(total_bytes * self.disk_warn_free_ratio, self.disk_warn_free_cap_bytes)

    def disk_critical_bytes(self, total_bytes: int) -> float:
        """Free-space level below which the hub also stops accepting telemetry."""
        return max(
            self.disk_min_free_bytes,
            min(total_bytes * self.disk_critical_free_ratio, self.disk_critical_free_cap_bytes),
        )

    @property
    def tls_enabled(self) -> bool:
        return bool(self.tls_cert and self.tls_key)
